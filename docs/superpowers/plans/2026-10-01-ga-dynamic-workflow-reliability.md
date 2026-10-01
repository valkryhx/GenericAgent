# GA Dynamic Workflow Reliability Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 GA 的 subagent、wait、workflow 调度和验收从“依赖模型猜测”升级为可持久化、可观测、可恢复的硬约束运行时，使串行、并行、链式和多步骤 workflow 在真实 `deepseek-v4.1-flash` 下稳定完成。

**Architecture:** 采用分层实现：Step-Code 负责 workflow runtime 的 schema/ACL/DAG/journal/resume 思路，ultracode-skill 负责显式 verification/evidence contract，Pi 负责 single/parallel/chain 子进程拓扑与生命周期，Codex CLI 负责 agent graph、thread/turn 状态和控制面语义。`taskType` 只保留为 planner 提示，不再直接决定 verification 门禁；所有完成、失败、跳过和结果可读性由 runtime 的结构化状态与 predicate 判定。

**Tech Stack:** Python 3.10-3.13、标准库 `dataclasses`/`unittest`/`subprocess`/`pathlib`、现有 `SubagentEventBus`、`SubagentRegistry`、`WorkflowStore`、`WorkflowScheduler`、真实 `deepseek-v4.1-flash` API（仅串行 E2E 验证，不在仓库保存凭据）。

---

## 当前执行状态（2026-10-01）

- Phase 1 wait predicate、durable result view 已完成并有回归测试。
- Phase 2 verification contract、runtime evidence、受限 check adapter 已完成；planner 已移除按 `taskType` 自动注入 verifier/schema 的旧逻辑。
- Phase 3 scheduler barrier、失败传播、显式容量策略已完成。
- 本 checkpoint 聚焦套件 322 tests passed；真实 deepseek 串行 E2E、journal/resume 深化和最终全量套件仍待后续阶段。

## 设计不变量

实施过程中必须保持以下不变量；每个不变量都要有自动化回归测试。

1. `wait_agent` 等待的是明确 predicate，不因 `agent_started`、`turn_started` 或任意日志变化提前宣告任务完成。
2. `wait_agent` 只负责等待和返回结构化状态；`read_agent_result` 负责读取正文，但 wait 返回值必须明确 `result_available`、`result_ref` 和下一步动作。
3. `turn_status`、`process_status`、`result_status`、`acceptance_status`、`execution_outcome` 不合并成一个布尔值。
4. 关闭或 stale 的 agent 仍能通过持久化 result view 读取 artifact；活跃列表为空不表示结果丢失。
5. 写入型 workflow 至少声明一个可观测 verification check；不强制创建名为 `verification` 的 agent，也不强制所有项目运行 `python_unittest`。
6. workflow DAG 的依赖、并发、失败传播和 terminal barrier 由 runtime 硬执行，模型不能用自然语言绕过。
7. `maxAgents` 只受用户/配置显式上限和宿主资源安全上限约束，不得再使用隐藏的固定 `min(..., 5)` 截断；当容量不足时返回结构化拒绝原因。
8. reviewer/verification agent 的能力通过工具边界和 capability profile 执行，提示词只用于解释目标。
9. 对真实模型的两个以上验证用例必须串行执行，避免把 provider 延迟、Windows 进程启动和并发限流混为一个故障。

## 文件边界总览

| 文件 | 责任 | 本计划中的变化 |
|---|---|---|
| `subagent_wait.py` | wait predicate、结果包和状态聚合 | 新建，隔离等待语义 |
| `subagent_manager.py` | 子进程/事件/registry 的桥接 | 修改，支持 predicate、race-safe 订阅、durable view |
| `subagent_state.py` | state/events/artifact 持久化 | 修改，增加 result 状态和版本兼容 |
| `subagent_registry.py` | 活跃与关闭 agent 查询 | 修改，提供统一 graph/result 查询 |
| `ga.py` | 工具参数解析和模型可见输出 | 修改，暴露结构化 wait 结果和错误 |
| `workflow_models.py` | workflow/job/check 状态模型 | 修改，增加 verification contract 与 outcome |
| `workflow_verification.py` | contract 归一化、legacy 转换、evidence | 新建，消除 planner/runtime 重复规则 |
| `workflow_check_adapters.py` | command/schema/artifact 检查执行 | 新建，受权限和 workspace 限制 |
| `workflow_planner.py` | 计划生成与静态校验 | 修改，taskType 降级为提示 |
| `workflow_policy.py` | 预算、权限、容量和 capability | 修改，去除隐藏 maxAgents 截断 |
| `workflow_scheduler.py` | DAG barrier、wave、失败传播 | 修改，硬性等待依赖 terminal |
| `workflow_runtime.py` | 运行、恢复、验收和总体 outcome | 修改，统一检查和结果投影 |
| `workflow_child_agent.py` | child 执行和结果封装 | 修改，区分 exit/turn/result/acceptance |
| `workflow_permissions.py` | capability/profile 工具边界 | 修改，加入 verify capability |
| `workflow_store.py` | workflow journal、result、resume | 修改，持久化 check evidence 和 decision packet |
| `subagent_prompts.py` | planner/worker/wait ergonomics | 修改，提示词不再承担硬门禁 |
| `tests/test_subagent_wait.py` | wait predicate 单测 | 新建 |
| `tests/test_workflow_verification.py` | contract/evidence 单测 | 新建 |
| `tests/test_workflow_check_adapters.py` | 检查适配器单测 | 新建 |
| `tests/test_workflow_capacity.py` | maxAgents 和资源拒绝单测 | 新建 |
| `tests/real_subagent_wait_terminal_e2e.py` | 真实子 agent wait 验证 | 新建，默认显式启用 |
| `tests/real_workflow_wait_barrier_e2e.py` | 真实 workflow barrier 验证 | 新建，默认显式启用 |
| `docs/20261001-workflow-reference-architecture-comparison.md` | 调研依据 | 保留并在实现中引用 |
| `docs/superpowers/plans/2026-10-01-ga-dynamic-workflow-reliability.md` | 执行计划 | 本文件 |

---

## Phase 0：建立基线与兼容边界

### Task 1: 固化当前行为和迁移样本

**Files:**
- Modify: `tests/test_subagent_manager.py`
- Modify: `tests/test_ga_subagent_tools.py`
- Modify: `tests/test_workflow_plan_validator.py`
- Create: `tests/fixtures/workflow_legacy_coding_plan.json`
- Create: `tests/fixtures/workflow_inline_verification_plan.json`

- [ ] **Step 1: 写出当前 wait 提前返回的回归测试**

```python
def test_wait_event_mode_does_not_claim_terminal_on_turn_started(manager):
    manager.emit_test_event("child", "agent_started")
    manager.emit_test_event("child", "turn_started")
    result = manager.wait_agents(
        targets=["child"],
        timeout_s=0,
        wait_condition="turn_terminal",
        since_event_seq=0,
    )
    assert result.timed_out is True
    assert result.condition == "turn_terminal"
    assert result.remaining_targets == ["child"]
```

- [ ] **Step 2: 写出 legacy coding plan 的快照测试**

```python
def test_legacy_coding_plan_is_loaded_without_mutating_fixture():
    plan = load_fixture("workflow_legacy_coding_plan.json")
    normalized = normalize_verification_contract(plan)
    assert normalized["checks"]
    assert normalized["metadata"]["legacyConverted"] is True
    assert plan["acceptance"]["checks"] == ["python_unittest", "verification_schema"]
```

- [ ] **Step 3: 运行基线测试并保存失败/通过结果**

Run: `python -m unittest tests.test_subagent_manager tests.test_ga_subagent_tools tests.test_workflow_plan_validator -v`

Expected: 当前基线测试完成；若新增测试因接口尚不存在而失败，失败信息必须明确指向待实现接口。

- [ ] **Step 4: 检查工作树和计划输入**

Run: `git diff --check && git status --short`

Expected: 不修改用户已有改动；记录未跟踪调研文档，不删除、不覆盖。

---

## Phase 1：重做 subagent wait 和 durable lifecycle

### Task 2: 引入结构化 wait predicate 和 decision packet

**Files:**
- Create: `subagent_wait.py`
- Modify: `subagent_manager.py:118-150,1215-1335`
- Modify: `subagent_state.py`
- Test: `tests/test_subagent_wait.py`

- [ ] **Step 1: 先写 wait 类型和 predicate 的失败测试**

```python
def test_wait_condition_all_terminal_tracks_remaining_targets():
    states = [
        fake_state("a", turn_status="completed", process_status="exited", result_status="available"),
        fake_state("b", turn_status="running", process_status="running", result_status="pending"),
    ]
    decision = evaluate_wait_condition(states, "all_terminal")
    assert decision.satisfied is False
    assert decision.remaining_targets == ["b"]
    assert decision.recommended_next_action == "wait_agent"


def test_result_available_requires_persisted_result_ref():
    state = fake_state("a", turn_status="completed", process_status="exited", result_status="pending")
    decision = evaluate_wait_condition([state], "result_available")
    assert decision.satisfied is False
    assert decision.recommended_next_action == "read_agent_result"
```

- [ ] **Step 2: 实现最小数据结构**

```python
from dataclasses import dataclass, field

WAIT_CONDITIONS = frozenset({
    "event", "turn_terminal", "process_terminal", "all_terminal",
    "result_available", "workflow_terminal",
})

@dataclass(frozen=True)
class WaitDecision:
    condition: str
    satisfied: bool
    timed_out: bool = False
    changed_agents: list = field(default_factory=list)
    remaining_targets: list[str] = field(default_factory=list)
    terminal_targets: list[str] = field(default_factory=list)
    result_refs: dict[str, str] = field(default_factory=dict)
    recommended_next_action: str = "wait_agent"
    reason: str = ""

def evaluate_wait_condition(states, condition):
    if condition not in WAIT_CONDITIONS:
        raise ValueError(f"unsupported wait condition: {condition}")
    terminal = [s for s in states if s.turn_status in {"completed", "failed", "cancelled", "killed", "stale"}]
    remaining = [s.task_name for s in states if s not in terminal]
    refs = {s.task_name: s.final_output_path for s in terminal if s.final_output_path}
    if condition == "event":
        satisfied = bool(states)
    elif condition == "turn_terminal":
        satisfied = len(terminal) == len(states)
    elif condition == "process_terminal":
        satisfied = all(s.process_status in {"exited", "shutdown", "killed"} for s in states)
    elif condition in {"all_terminal", "workflow_terminal"}:
        satisfied = len(remaining) == 0
    else:
        satisfied = bool(states) and len(refs) == len(states)
    return WaitDecision(
        condition=condition,
        satisfied=satisfied,
        remaining_targets=remaining,
        terminal_targets=[s.task_name for s in terminal],
        result_refs=refs,
        recommended_next_action="read_agent_result" if refs and satisfied else "wait_agent",
        reason="terminal predicate satisfied" if satisfied else "waiting for remaining targets",
    )
```

- [ ] **Step 3: 扩展 `WaitResult` 并接入 manager**

```python
@dataclass
class WaitResult:
    timed_out: bool
    changed_agents: list[AgentState]
    message: str
    events: list[dict] | None = None
    next_event_seq: int | None = None
    observed_agents: list[AgentState] = field(default_factory=list)
    condition: str = "event"
    satisfied: bool = False
    remaining_targets: list[str] = field(default_factory=list)
    terminal_targets: list[str] = field(default_factory=list)
    result_refs: dict[str, str] = field(default_factory=dict)
    recommended_next_action: str = "wait_agent"
```

`wait_agents()` 增加 `wait_condition="event"` 参数，先读取 baseline 后订阅 event bus，再执行一次状态 fast path；循环中只有满足 predicate 才返回。事件到达但 predicate 未满足时只更新 baseline，继续等待直到 timeout。

- [ ] **Step 4: 验证 race-safe 行为**

Run: `python -m unittest tests.test_subagent_wait tests.test_subagent_manager -v`

Expected: `turn_started` 不满足 `turn_terminal`；两个目标中一个完成时 `all_terminal` 返回 `remaining_targets`；已有 result ref 时返回 `recommended_next_action=read_agent_result`。

### Task 3: 暴露 wait API 并保持工具职责分离

**Files:**
- Modify: `ga.py:987-1035`
- Modify: `subagent_manager.py:2309-2325`
- Modify: `tests/test_ga_subagent_tools.py`

- [ ] **Step 1: 为工具参数写失败测试**

```python
def test_do_wait_agent_returns_structured_terminal_packet(ga, manager):
    manager.complete_agent("alpha", result_path="artifacts/alpha/final_output.json")
    response = ga.do_wait_agent(
        {"targets": ["alpha"], "condition": "result_available", "timeout_s": 0},
        {},
    )
    assert response["satisfied"] is True
    assert response["resultRefs"]["alpha"].endswith("final_output.json")
    assert response["recommendedNextAction"] == "read_agent_result"
```

- [ ] **Step 2: 实现兼容参数映射**

```python
condition = str(args.get("condition") or args.get("waitCondition") or "event")
result = self.subagent_manager.wait_agents(
    targets=args.get("targets"),
    timeout_s=float(args.get("timeout_s", args.get("timeoutSeconds", 30))),
    since_event_seq=args.get("sinceEventSeq"),
    wait_condition=condition,
)
return {
    "timedOut": result.timed_out,
    "condition": result.condition,
    "satisfied": result.satisfied,
    "remainingTargets": result.remaining_targets,
    "terminalTargets": result.terminal_targets,
    "resultRefs": result.result_refs,
    "recommendedNextAction": result.recommended_next_action,
    "message": result.message,
    "events": result.events or [],
    "nextEventSeq": result.next_event_seq,
}
```

- [ ] **Step 3: 保持 `read_agent_result` 独立**

增加测试证明 wait 不读取正文，只返回 ref；`read_agent_result` 仍从 artifact/state 读取完整内容，即使 registry 默认活跃列表为空。

Run: `python -m unittest tests.test_ga_subagent_tools tests.test_subagent_artifacts tests.test_subagent_registry -v`

Expected: wait packet 可指导下一次工具调用；关闭 agent 仍可读持久化结果。

### Task 4: 统一 active/closed/result/graph 视图

**Files:**
- Modify: `subagent_registry.py`
- Modify: `subagent_state.py`
- Modify: `subagent_manager.py`
- Modify: `tests/test_subagent_registry.py`
- Modify: `tests/test_subagent_artifacts.py`

- [ ] **Step 1: 写出 closed agent 可查询的测试**

```python
def test_result_view_includes_closed_agent_without_reviving_process(registry, closed_state):
    registry.save(closed_state)
    rows = registry.list_agents(include_closed=False)
    assert rows == []
    result_rows = registry.list_result_view(include_closed=True)
    assert [row.task_name for row in result_rows] == [closed_state.task_name]
    assert result_rows[0].process_status == "exited"
```

- [ ] **Step 2: 增加明确查询接口**

```python
def list_result_view(self, path_prefix=None, include_closed=True):
    return self.list_agents(path_prefix=path_prefix, include_closed=include_closed)

def list_descendants(self, parent_session_id):
    rows = self.list_agents(include_closed=True)
    return [row for row in rows if row.parent_session_id == parent_session_id]
```

结果 view 只读 state/artifact，不启动、不恢复、不改变 registry 状态。

- [ ] **Step 3: 给 `AgentState` 增加 result 字段并兼容旧 state**

增加 `result_status: str = "pending"`、`result_ref: str | None = None`、`result_checksum: str | None = None`。读取旧 JSON 时缺失字段采用 pending 和已有 `final_output_path` 推导 available；写入 state 时保留 schema version。

- [ ] **Step 4: 验证重启和 stale 场景**

Run: `python -m unittest tests.test_subagent_registry tests.test_subagent_artifacts tests.test_subagent_manager -v`

Expected: 进程退出、registry closed/stale、manager 重建后仍能列出结果 ref 并读取正文；不会因为读取 result view 而误判为 live。

---

## Phase 2：显式 verification/evidence contract

### Task 5: 建立 contract 模型与 legacy 转换

**Files:**
- Create: `workflow_verification.py`
- Modify: `workflow_models.py`
- Modify: `workflow_planner.py`
- Test: `tests/test_workflow_verification.py`

- [ ] **Step 1: 写出 contract 归一化测试**

```python
def test_inline_contract_does_not_require_verification_role():
    plan = {
        "agents": [{"id": "impl", "role": "implementation", "writeScope": ["src"]}],
        "verification": {
            "level": "inline",
            "checks": [{"id": "targeted", "kind": "command", "required": True, "owner": "host", "command": "python -m unittest tests.test_x"}],
        },
    }
    contract = normalize_verification_contract(plan)
    assert contract["level"] == "inline"
    assert contract["independentReview"] is False
    assert contract["checks"][0]["kind"] == "command"


def test_legacy_fields_convert_without_forcing_new_roles():
    plan = load_fixture("workflow_legacy_coding_plan.json")
    contract = normalize_verification_contract(plan)
    ids = {check["id"] for check in contract["checks"]}
    assert "legacy-python-unittest" in ids
    assert "legacy-verification-schema" in ids
    assert contract["metadata"]["legacyConverted"] is True
```

- [ ] **Step 2: 实现 contract schema 和兼容转换**

```python
VERIFICATION_LEVELS = frozenset({"none", "inline", "full"})
CHECK_KINDS = frozenset({"command", "schema", "review", "diff", "manual", "artifact"})
CHECK_OWNERS = frozenset({"host", "implementation", "independent_agent"})

def normalize_verification_contract(plan):
    raw = plan.get("verification") if isinstance(plan, dict) else None
    if isinstance(raw, dict):
        level = str(raw.get("level") or "inline")
        checks = normalize_checks(raw.get("checks") or [])
        independent = bool(raw.get("independentReview", False))
        return {"level": require_level(level), "checks": checks, "independentReview": independent, "metadata": {"legacyConverted": False}}
    acceptance = plan.get("acceptance") if isinstance(plan, dict) else None
    checks = []
    for item in (acceptance.get("checks") if isinstance(acceptance, dict) else []) or []:
        name = str(item.get("type") if isinstance(item, dict) else item)
        if name == "python_unittest":
            checks.append({"id": "legacy-python-unittest", "kind": "command", "required": True, "owner": "host", "adapter": "python_unittest"})
        elif name in {"verification", "verification_schema"}:
            checks.append({"id": "legacy-verification-schema", "kind": "schema", "required": True, "owner": "implementation", "schemaRef": "verification_result"})
    has_verifier = any(str(agent.get("role") or "").lower() == "verification" for agent in plan.get("agents") or [])
    return {"level": "full" if has_verifier else "inline", "checks": checks, "independentReview": has_verifier, "metadata": {"legacyConverted": True}}
```

`normalize_checks()` 必须拒绝未知 kind、缺失 id、非布尔 required、非法 owner，并把 `verification_schema` 仅作为 legacy 输入转换，不再作为新的硬编码检查名。

- [ ] **Step 3: 将 contract 放入 `WorkflowRun.metadata` 并保持序列化兼容**

在 `WorkflowRun.to_dict/from_dict` 中保留 `verificationContract`；旧 run 读取时调用归一化函数，写回时同时保留原始 `acceptanceContract` 供回滚诊断。

- [ ] **Step 4: 运行 contract 单测**

Run: `python -m unittest tests.test_workflow_verification tests.test_workflow_models -v`

Expected: 新 contract、legacy contract、非法 contract、空 checks、full contract 的错误均有明确断言。

### Task 6: 将 acceptance 评估改为 evidence-driven

**Files:**
- Create: `workflow_check_adapters.py`
- Modify: `workflow_runtime.py:633-700`
- Modify: `workflow_store.py`
- Test: `tests/test_workflow_check_adapters.py`
- Test: `tests/test_workflow_runtime.py`

- [ ] **Step 1: 写检查适配器测试**

```python
def test_python_unittest_adapter_records_exit_code_and_output(tmp_path):
    result = run_check(
        {"id": "tests", "kind": "command", "adapter": "python_unittest", "required": True,
         "command": "python -m unittest discover -s tests"},
        workspace=tmp_path,
        timeout_s=5,
    )
    assert result["checkId"] == "tests"
    assert result["evidence"]["exitCode"] == 0
    assert result["status"] == "passed"


def test_command_adapter_rejects_workspace_escape(tmp_path):
    with self.assertRaisesRegex(ValueError, "workspace"):
        run_check({"id": "bad", "kind": "command", "command": "cd .. && del file"}, workspace=tmp_path, timeout_s=5)
```

- [ ] **Step 2: 实现受限 adapter 接口**

```python
def run_check(check, *, workspace, timeout_s):
    validate_check_command(check, workspace)
    if check.get("kind") == "schema":
        return evaluate_schema_check(check)
    if check.get("kind") == "artifact":
        return evaluate_artifact_check(check, workspace)
    command = resolve_command(check)
    completed = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=timeout_s, shell=False)
    return {
        "checkId": check["id"],
        "status": "passed" if completed.returncode == 0 else "failed",
        "evidence": {"exitCode": completed.returncode, "stdout": redact(completed.stdout), "stderr": redact(completed.stderr)},
    }
```

首批 adapter 只支持参数化 executable/argv 和 `python_unittest` 规范入口；禁止把任意字符串直接交给 shell。所有 evidence 写入 workflow artifact，并包含 command fingerprint、开始/结束时间、exit code、超时和截断标记。

- [ ] **Step 3: 替换 runtime 固定检查分支**

`WorkflowRuntime._evaluate_acceptance()` 改为遍历 normalized contract 的 required checks，读取对应 evidence，缺 evidence 为 failed，明确 skipped 原因；保留旧 `python_unittest`/`verification_schema` 读取作为 migration fallback，但新计划不再生成这些名字。

- [ ] **Step 4: 验证 inline/full/none 三档**

Run: `python -m unittest tests.test_workflow_check_adapters tests.test_workflow_runtime tests.test_workflow_store -v`

Expected: `none` 不产生错误门禁；`inline` 只要求声明的 required checks；`full` 缺 independent evidence 时失败；失败 evidence 会让 workflow outcome 为 failed，而不是 succeeded/partial。

### Task 7: planner 降级 taskType 并移除固定 coding 门禁

**Files:**
- Modify: `workflow_planner.py:916-1025`
- Modify: `workflow_policy.py`
- Modify: `subagent_prompts.py`
- Test: `tests/test_workflow_plan_validator.py`

- [ ] **Step 1: 写出研究型和代码型计划的对比测试**

```python
def test_research_plan_is_not_forced_to_create_verification_agent():
    plan = make_plan(agent_role="research", writes=False, checks=[{"id": "sources", "kind": "artifact", "required": True, "owner": "host"}])
    issues = validate_workflow_plan(plan)
    assert not any(item["code"] == "missing_verification_role" for item in issues)


def test_write_plan_requires_observable_check_but_not_python_unittest():
    plan = make_plan(agent_role="implementation", writes=True, checks=[{"id": "diff", "kind": "diff", "required": True, "owner": "host"}])
    issues = validate_workflow_plan(plan)
    assert issues == []


def test_write_plan_without_required_check_is_rejected():
    plan = make_plan(agent_role="implementation", writes=True, checks=[])
    issues = validate_workflow_plan(plan)
    assert any(item["code"] == "missing_verification_check" for item in issues)
```

- [ ] **Step 2: 实现 plan-shape 规则**

保留 `plan_produces_code(plan)` 作为风险提示和默认 level 输入；验证逻辑改为：计划存在 write scope 或 write-capable agent 时，normalized contract 必须有至少一个 `required=true` check；只有明确 `independentReview=true` 或 `owner=independent_agent` 时才要求独立审查 agent/capability。`taskType` 不再直接添加 verifier、schema 或 Python test 要求。

- [ ] **Step 3: 更新 planner prompt**

提示词必须明确：研究/审阅任务使用 artifact/schema/diff 等最小证据；普通代码任务可用 inline check；公共 API、schema、迁移、共享模块、多写者才建议 full；不要因为 `taskType=coding` 自动生成 `verification` agent 或 `python_unittest`。

- [ ] **Step 4: 运行计划校验回归**

Run: `python -m unittest tests.test_workflow_plan_validator tests.test_workflow_policy -v`

Expected: 旧 fixture 通过 legacy conversion；新的研究型、代码型、full 风险型计划分别得到预期门禁。

---

## Phase 3：DAG barrier、失败传播和容量策略

### Task 8: 让 scheduler 硬执行 dependency barrier

**Files:**
- Modify: `workflow_scheduler.py`
- Modify: `workflow_models.py`
- Modify: `workflow_runtime.py`
- Modify: `workflow_child_agent.py`
- Test: `tests/test_workflow_scheduler.py`
- Test: `tests/test_workflow_runtime.py`

- [ ] **Step 1: 写 barrier 和失败传播测试**

```python
def test_dependent_job_cannot_start_before_all_dependencies_terminal(scheduler):
    run = make_run(jobs=[job("a"), job("b", depends_on=["a"])])
    scheduler.mark_running(run, "a")
    assert scheduler.ready_jobs(run) == []
    scheduler.complete(run, "a", status="succeeded")
    assert [item.job_id for item in scheduler.ready_jobs(run)] == ["b"]


def test_failed_dependency_marks_downstream_blocked(scheduler):
    run = make_run(jobs=[job("a"), job("b", depends_on=["a"])])
    scheduler.complete(run, "a", status="failed")
    scheduler.reconcile(run)
    assert run.job("b").status == "skipped"
    assert run.job("b").metadata["blockedBy"] == ["a"]
```

- [ ] **Step 2: 实现 terminal predicate 和 wave barrier**

调度器只把依赖状态在 `{succeeded, cached}` 且所有 required checks 已有 evidence 的 job 放入 ready；`failed/cancelled/killed/stale/skipped` 作为 terminal failure/blocked 输入，不允许下游以空结果启动。parallel wave 必须等待本 wave 所有 required jobs terminal，再释放下一 wave。

- [ ] **Step 3: 实现 workflow outcome 投影**

`WorkflowRun.metadata["executionOutcome"]` 只能由 runtime 根据 job summary 和 acceptance status 计算：`succeeded`、`partial`、`failed`、`aborted`、`killed`；禁止把 failed child 当作自然语言摘要吞掉。

- [ ] **Step 4: 验证恢复和重复 reconcile 幂等性**

Run: `python -m unittest tests.test_workflow_scheduler tests.test_workflow_runtime tests.test_workflow_models -v`

Expected: 重复 reconcile 不重复启动 job、不重复写 blocked 事件；恢复 run 后仍按 dependency barrier 继续。

### Task 9: 去除隐藏 maxAgents=5，建立显式容量决策

**Files:**
- Modify: `workflow_policy.py`
- Modify: `workflow_scheduler.py`
- Modify: `workflow_planner.py`
- Modify: `ga.py`
- Create: `tests/test_workflow_capacity.py`

- [ ] **Step 1: 写容量测试**

```python
def test_requested_max_agents_is_preserved_when_within_configured_limit():
    policy = normalize_orchestration_policy({"maxAgents": 12}, capacity=32)
    assert policy["maxAgents"] == 12


def test_capacity_rejection_is_structured_when_limit_is_exceeded():
    with self.assertRaisesRegex(WorkflowCapacityError, "requested 40 agents"):
        normalize_delegation_policy({"orchestration": {"maxAgents": 40}}, capacity=16)
```

- [ ] **Step 2: 实现显式容量来源和错误**

在 `workflow_policy.py` 新增 `WorkflowCapacityError(ValueError)`，并将现有 `normalize_delegation_policy()` 扩展为接受 `capacity` 参数。容量拆成 `requestedMaxAgents`、`configuredMaxAgents`、`resourceMaxAgents`、`effectiveMaxAgents`；只有配置或资源探针提供上限时才限制。若请求超过上限，抛出包含 requested/configured/resource/effective 的结构化错误；不静默改成 5。

- [ ] **Step 3: 将容量决策写入 workflow journal**

每次 run 初始化记录 `capacityDecision`，包含来源、时间、当前并发和拒绝原因；模型可见摘要只显示安全字段，完整诊断写 artifact。

- [ ] **Step 4: 运行容量回归**

Run: `python -m unittest tests.test_workflow_capacity tests.test_workflow_policy tests.test_workflow_scheduler -v`

Expected: 12 个 agent 请求不会被截成 5；超过显式容量时在启动前失败，且不产生半启动 child。

### Task 10: 增加 verify capability 和工具边界

**Files:**
- Modify: `workflow_permissions.py`
- Modify: `workflow_child_agent.py`
- Modify: `workflow_scheduler.py`
- Test: `tests/test_workflow_permissions.py`
- Test: `tests/test_workflow_runtime.py`

- [ ] **Step 1: 写 capability 测试**

```python
def test_verify_capability_allows_read_search_and_safe_checks_but_denies_write(policy):
    assert policy.allows("read_file")
    assert policy.allows("search")
    assert policy.allows("run_check")
    assert not policy.allows("write_file")
    assert not policy.allows("patch")
```

- [ ] **Step 2: 实现 capability profile**

新增 `verify` profile，默认允许 read/search/list、受限测试命令和 artifact 写入；拒绝 edit/patch/delete/迁移。`independent_agent` check 只能绑定 verify profile 或更严格 profile。

- [ ] **Step 3: 验证拒绝发生在工具边界**

Run: `python -m unittest tests.test_workflow_permissions tests.test_workflow_runtime -v`

Expected: 即使 child prompt 要求写文件，工具调用也在 permission layer 被拒绝并记录 evidence；提示词不能升级能力。

---

## Phase 4：planner/tool ergonomics 和可恢复运行

### Task 11: 让模型正确使用 wait/result packet

**Files:**
- Modify: `subagent_prompts.py`
- Modify: `ga.py`
- Modify: `workflow_runtime.py`
- Test: `tests/test_ga_subagent_tools.py`
- Test: `tests/test_workflow_runtime.py`

- [ ] **Step 1: 写工具协议测试**

```python
def test_timeout_packet_instructs_wait_without_respawn():
    packet = make_wait_packet(timed_out=True, remaining_targets=["child"], result_refs={})
    assert packet["recommendedNextAction"] == "wait_agent"
    assert packet["retrySpawn"] is False


def test_terminal_packet_instructs_read_result():
    packet = make_wait_packet(satisfied=True, remaining_targets=[], result_refs={"child": "r.json"})
    assert packet["recommendedNextAction"] == "read_agent_result"
```

- [ ] **Step 2: 更新 prompt 和错误摘要**

明确说明：`event` 是观察更新，`all_terminal` 才是等待全部；timeout 不等于失败，不要重复 spawn；closed/stale agent 先调用 result view/read；process exit 与 result availability 分开判断。

- [ ] **Step 3: 统一 workflow runtime 的 RPC/child 等待**

runtime 内部使用同一组 terminal predicates；不得通过 `list -> sleep -> list -> read` 的自然语言循环替代 scheduler barrier。每个等待事件写入 journal，包含 predicate、targets、remaining 和 deadline。

- [ ] **Step 4: 运行工具协议回归**

Run: `python -m unittest tests.test_ga_subagent_tools tests.test_workflow_runtime tests.test_subagent_wait -v`

Expected: 模拟 timeout、partial terminal、all terminal 和 result missing 场景都返回确定的下一步动作。

### Task 12: 增加 journal/checkpoint/resume 语义

**Files:**
- Modify: `workflow_store.py`
- Modify: `workflow_runtime.py`
- Modify: `subagent_state.py`
- Test: `tests/test_workflow_store.py`
- Test: `tests/test_workflow_runtime.py`

- [ ] **Step 1: 写 resume 测试**

```python
def test_resume_keeps_terminal_jobs_and_only_requeues_unfinished(tmp_path):
    run = make_run_with_checkpoint(tmp_path, statuses={"a": "succeeded", "b": "running", "c": "queued"})
    runtime = WorkflowRuntime(store=WorkflowStore(root=tmp_path), runner=FakeChildAgentRunner())
    resumed = runtime.run(run, resume_from_run_id=run.run_id)
    assert resumed.job("a").status == "succeeded"
    assert resumed.job("b").status in {"queued", "stale"}
    assert resumed.job("c").status == "queued"
```

- [ ] **Step 2: 实现原子 journal 记录**

每次状态转换写 `workflow_event`、job status、result ref、check evidence ref；恢复时读取最后一个合法 checkpoint，验证 result checksum，禁止重复执行已成功且证据仍有效的 job。

- [ ] **Step 3: 验证进程中断恢复**

Run: `python -m unittest tests.test_workflow_store tests.test_workflow_runtime tests.test_subagent_artifacts -v`

Expected: manager/runtime 重启不丢结果、不重复执行成功 child；损坏 artifact 被标记 stale 并要求重新执行。

---

## Phase 5：真实模型串行验收和性能观测

### Task 13: 增加真实 deepseek-v4.1-flash wait E2E

**Files:**
- Create: `tests/real_subagent_wait_terminal_e2e.py`
- Create: `tests/real_workflow_wait_barrier_e2e.py`
- Modify: `docs/20261001-workflow-reference-architecture-comparison.md`

- [ ] **Step 1: 定义显式 opt-in 的开发领域任务**

`real_subagent_wait_terminal_e2e.py` 使用真实 `deepseek-v4.1-flash`，任务为“读取仓库中一个 Python 模块，列出两个潜在边界条件并写入指定 artifact”，不要求修改源码；验证两个 child 串行启动、`turn_started` 不提前满足 terminal、`all_terminal` 返回两个 result ref。

`real_workflow_wait_barrier_e2e.py` 使用真实模型执行中等难度开发任务：第一步读取并分析一个模块，第二步依赖第一步产出的 artifact 生成测试建议，第三步由 host 运行受限检查并写 markdown 汇总；包含文件读写和一个 MCP 调用时才启用对应 fixture。

- [ ] **Step 2: 实现 opt-in 和脱敏**

测试只在 `GA_RUN_REAL_E2E=1` 且模型配置已存在时运行；API key 从环境/本地配置读取，不写入输出、fixture 或 commit；每个用例写运行时长、startup phases、wait predicates、event sequence 和 outcome 到临时 artifact。

- [ ] **Step 3: 两个用例严格串行执行**

Run: `GA_RUN_REAL_E2E=1 python -m unittest tests.real_subagent_wait_terminal_e2e -v`，完成后再运行 `GA_RUN_REAL_E2E=1 python -m unittest tests.real_workflow_wait_barrier_e2e -v`。

Expected: 第一用例证明等待不会在 `agent_started/turn_started` 提前返回；第二用例证明依赖 barrier、result ref、verification evidence 和失败传播均由 runtime 执行。

- [ ] **Step 4: 添加失败分类报告**

报告至少区分：startup handshake、provider latency、MCP latency、wait predicate、scheduler barrier、artifact persistence、verification evidence、permission denial。timeout 只记录为 observation，不自动判定实现失败，除非 terminal predicate 或持久化契约违反。

### Task 14: 增加本地全量验证和性能基线

**Files:**
- Modify: `docs/20261001-workflow-reference-architecture-comparison.md`
- Create: `docs/20261001-ga-dynamic-workflow-reliability-validation.md`

- [ ] **Step 1: 运行单元测试和静态检查**

Run: `python -m unittest discover -s tests -v`；`git diff --check`。

Expected: 现有测试全部通过；新测试覆盖 wait、verification、scheduler、capacity、permissions、resume。

- [ ] **Step 2: 记录分阶段指标**

记录每个真实 run 的 spawn-to-process-entry、process-entry-to-turn-started、turn duration、MCP duration、wait return count、重复 spawn 次数、result persistence latency、workflow total latency。不得只用总耗时推断 provider 或 GA 根因。

- [ ] **Step 3: 写验证结论**

文档必须明确哪些指标是 GA 可控、哪些由模型/provider 决定；如果失败，引用具体 event sequence 和 state/artifact 文件，而不是用“模型不稳定”作为唯一结论。

---

## 兼容、发布和回滚策略

1. 第一阶段保留旧 `acceptanceContract` 读取，所有旧计划经过 `normalize_verification_contract()` 后进入新 runtime；新计划只写 `verification`。
2. wait API 保留默认 `condition="event"` 以兼容旧调用，但 planner 和新增工具调用必须显式使用 `turn_terminal`、`all_terminal` 或 `result_available`。
3. 旧状态文件缺少 result 字段时按已有 output/final artifact 推导，不回收、不覆盖旧日志。
4. 新 scheduler 先在 feature flag 下启用 `strict_barrier`；验证通过后将其设为默认，保留一版可读的兼容诊断开关。
5. 每个任务完成后提交独立 Conventional Commit，例如 `feat(subagent): add terminal wait predicates`、`feat(workflow): add evidence contract`、`fix(workflow): enforce dependency barrier`。
6. 不提交真实 API key、运行 artifact、provider transcript 或本机路径中的敏感配置。

## 最终验收标准

- [ ] `wait_agent(condition="all_terminal")` 在所有目标 terminal 前不返回 satisfied。
- [ ] timeout 返回 remaining targets 和下一步动作，不触发重复 spawn。
- [ ] closed/stale agent 仍能从 result view/read 读取持久化结果。
- [ ] 研究型 workflow 不被强制转为代码型 verification contract。
- [ ] 写入型 workflow 没有 required check 时被 runtime 拒绝；有任意可观测 required check 时不再强制 `verification agent`、`python_unittest` 或 `verification_schema`。
- [ ] full contract 的 independent review 由 capability 和 evidence 硬执行。
- [ ] dependency 未 terminal 时下游不启动；上游失败时下游变为 blocked/skipped。
- [ ] `maxAgents` 不被隐藏截成 5；超过显式容量时结构化拒绝且无半启动进程。
- [ ] 重启后成功 job 不重复执行，损坏 result 被识别并重新排队。
- [ ] 两个真实 deepseek-v4.1-flash 用例串行通过，且诊断报告能区分 GA 与 provider 延迟。

## 执行方式

计划已拆成可独立验证的任务，建议采用 Subagent-Driven 执行：每个 Task 独立实现、运行聚焦测试并提交 checkpoint，再由主 agent 做跨任务集成验证。若需要保持单一上下文，也可使用 Inline Execution 按 Phase 顺序执行；无论选择哪种方式，Phase 1 完成前不要开始 Phase 2，Phase 2/3 的 contract 和 scheduler 先通过单测再运行真实模型。
