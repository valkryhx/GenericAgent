# GA Workflow LLM 接入对齐 `llm.yaml` 修改方案

日期：2026-07-22
分支：`feat/dynamic-workflows-foundation`
范围：workflow 运行时 / child subagent / planner / Ink bridge 默认接线 / 相关单测与 opt-in 真实 E2E
**不含**：主会话 `llm.yaml` 本身重构（阶段 3 已落地）；权限档位持久化；workflow child 人审 UI（已拍板不做）

相关现状文档：

- `docs/ga_llm_config_research_2026-07-16.md`（主会话 YAML 方案）
- `docs/dynamic-workflows-implementation-roadmap.md`
- `docs/GA_workflow_defect_optimization_plan.md`
- `docs/ga_workflow_permission_inherit_research_2026-07-21.md`（child full_access 拍板）

---

## 0. 一句话目标

把 workflow 的 LLM 接入从 **mykey + `native_oai_config` + 默认 Fake child**，改成与主会话一致的 **`llm.yaml` profile 体系**；生产路径默认跑 **真 subagent**；且 **workflow 内每个 subagent 直接沿用主会话当前 `/model` 选中的 profile/model**（中途 `/model` 切换后，后续新 job 跟新选择）。

---

## 1. 现状诊断（代码事实）

### 1.1 主会话：已是 `llm.yaml` 世界

| 组件 | 路径 | 语义 |
|------|------|------|
| 配置 | `llm.yaml` → `llm_config.load_llm_config` | providers / models / **profiles** / mixin / `active_profile` |
| 构造 | `llm_client.build_client(config, profile_name)` | profile → model → provider.wire_api → `Native*Session` + `NativeToolClient` |
| 装载 | `agentmain.load_clients_from_yaml` → `GenericAgent.load_llm_sessions` | **只读 yaml，不再读 mykey 分发** |
| 切换 | `/model` → `next_llm` / `_switch_llm_index` | 改 `llm_no` + `self.llmclient`；显示名 `profile/model` |
| 当前会话句柄 | `agent.llmclient`（`NativeToolClient`） | `backend.name` = profile；`backend.model` = 模型 id |

本机 `llm.yaml` 摘要（以仓库文件为准）：

```text
profiles: default, gpt, grok, glm, gpt-mini, hhhl-grok, terra
models: 含 grok-4.5、gpt-5.6-terra、gpt-5.4 …
active_profile: default
```

### 1.2 Workflow：仍是 mykey 旧世界 + 默认 Fake

| 组件 | 现状 | 问题 |
|------|------|------|
| `WorkflowRuntime.__init__` | `runner or FakeChildAgentRunner()` | **生产默认 child 不调 LLM、不跑工具**，直接 `completed agent_N` |
| `ink_bridge._make_workflow_runtime` | 只传 `store`（+timeout），**不传 runner** | Ink `/workflow plan|approve` 走 Fake |
| `NativeGPTChildAgentRunner` | 默认 `config_name="native_oai_config"`；`resolve_client/resolve_session` | 读 **mykey 字典键**，不认 profile 名 `grok`/`terra` |
| `workflow_planner.NativeWorkflowPlannerClient` | 同上 `resolve_session(config_name)` | planner 真 LLM 模式也旧 |
| `build_workflow_planner_from_env` | 默认 deterministic；env 真模式默认 `native_oai_config` | 与 `/model` 无关 |
| 真实 E2E 脚本 | `GA_REAL_API_CONFIG=native_oai_config` + 手写注入 Native runner | 证明「能真跑」，但 **未接到默认产品路径** |

本机验证摘录：

```text
resolve_session("native_oai_config") → OK（mykey）
resolve_session("grok"|"terra"|"grok-4.5") → Config not in mykey
bridge._make_workflow_runtime().runner → FakeChildAgentRunner
```

### 1.3 两套世界示意

```text
主会话 GenericAgent
  llm.yaml profiles ──build_client──► agent.llmclient (/model 可切换)
                                              │
                                              │  ❌ 当前无接线
                                              ▼
WorkflowRuntime 默认 ──FakeChildAgentRunner──► 假成功 payload
可选测试注入 ──NativeGPTChildAgentRunner(config_name=mykey键)──► 旧 resolve_*
```

### 1.4 严重性分级

| ID | 问题 | 级别 |
|----|------|------|
| W-LLM-1 | 默认 child = Fake，用户侧 workflow「假成功」 | **P0 产品缺陷** |
| W-LLM-2 | 真 child / planner 仍 mykey `resolve_*`，与 yaml 分家 | **P0 接入债务** |
| W-LLM-3 | child 不跟随主会话 `/model` | **P0 产品要求（本次拍板）** |
| W-LLM-4 | 单测/E2E 默认字符串 `native_oai_config` / 期望 gpt-5.5 等过时 | **P1 测试债务** |
| W-LLM-5 | planner 默认 deterministic；真模式 env 与 `/model` 脱节 | **P1**（可与 P0 同批设计） |

---

## 2. 产品规则（本次必须遵守）

1. **配置源唯一**：workflow 真 LLM 只走 `llm.yaml` + `llm_client.build_*`（或等价：复用已构造的 `NativeToolClient`）。**禁止**再以 mykey 变量名 / `resolve_session(cfg_name)` 作为 workflow 默认路径。
2. **Subagent 模型 = 主会话当前 `/model`**：
   - 以 `GenericAgent` 当前 `llm_no` / `llmclient` 为准（profile 名 = `backend.name`，model = `backend.model`）。
   - 用户中途 `/model` 切换后，**之后新启动的 workflow job** 使用新模型。
   - **不**要求改已经 running 的 in-flight job 的模型（实现简单、语义清晰）。
3. **默认真跑**：Ink / 非测试生产路径的 `WorkflowRuntime` **默认**使用真 child runner，不再默认 Fake。
4. **测试可注入**：单测继续显式 `FakeChildAgentRunner` 或 stub `client_factory`，保持无网、快速。
5. **权限正交**：workflow child 仍走 `workflow_permission_policy`（默认 inherit→allow / full_access 拍板）；主会话 `ask/read_only` **不**改 child 工具审批模型。
6. **密钥安全**：不读不打印 `mykey.py` / key 明文；真实 E2E 继续 opt-in 环境变量门闩。
7. **真实 API 偏好（运维约定，非代码写死）**：本机 live 验证优先 `llm.yaml` 的 **`grok` profile → `grok-4.5` / `grok-endpoint`**；避免把默认验证绑死在 `gpt-5.6-terra`（渠道限流备注已写在 yaml）。代码层只「跟 `/model`」，不写死 grok。

---

## 3. 目标架构

### 3.1 模型解析责任链

```text
                    ┌──────────────────────────────┐
                    │ GenericAgent                 │
                    │  llmclients[], llm_no        │
                    │  llmclient (current)         │
                    │  get_llm_name() /model       │
                    └──────────────┬───────────────┘
                                   │ snapshot / factory
                                   ▼
                    ┌──────────────────────────────┐
                    │ WorkflowLlmBinding（新）       │
                    │  - profile_name              │
                    │  - model_id                  │
                    │  - client_factory() → 新 client│
                    │  - planner_client_factory()  │
                    └──────────────┬───────────────┘
                                   │
           ┌───────────────────────┼───────────────────────┐
           ▼                       ▼                       ▼
 NativeWorkflowChildRunner   LLMWorkflowPlanner      capability metadata
 （每 job 新建 client/        （plan 时用同一 profile    （progress/journal
  独立 history）               或同 binding）             记录 profile/model）
```

### 3.2 关键设计决策

#### D1. Subagent **不要共享**主会话 `llmclient` 实例

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| A. 直接复用 `agent.llmclient` | 零构造 | 污染主会话 `backend.history` / tools / 并发 | **否** |
| B. 每 job `build_client(yaml, profile)` 新实例 | 隔离 transcript、可并发 | 多一次构造 | **是（默认）** |
| C. clone 主 client 再清 history | 可能复制内部状态 | clone API 不统一 | 不优先 |

**拍板：B**。profile 名从主会话读取；**每个 job 新建** `NativeToolClient`（独立 history）。
构造入口统一：`llm_client.build_client(load_llm_config(...), profile_name)`。

#### D2. 「沿用 `/model`」的快照时机

| 时机 | 语义 |
|------|------|
| **每个 child job `start` 时**读一次当前 binding | 最贴「直接沿用主会话当前设置」；中途 `/model` 只影响后续 job |
| runtime 启动时固定 profile | 实现简单，但长 workflow 中途切模型不生效 |

**拍板：job start 时解析当前 profile**（推荐）。
实现上 runner 持有 `binding_provider: Callable[[], WorkflowLlmBinding]`，每次 `_new_executable` 调一次。

#### D3. 无主会话上下文时（纯库调用 / 部分 E2E）

优先级：

1. 显式传入的 `client_factory` / `session_factory`（单测）
2. 显式 `profile_name`（脚本/env）
3. `llm.yaml` 的 `active_profile`
4. **禁止**再默认 `native_oai_config`

#### D4. 默认 runner：真 / 假

| 环境 | runner |
|------|--------|
| Ink bridge 生产 `_make_workflow_runtime` | **真** child（Native，绑定 agent） |
| `WorkflowRuntime()` 无参（库默认） | **真** child（绑定 yaml `active_profile`）；或要求显式 runner——二选一见 §4.2 |
| unittest | 显式 Fake / stub factory |

**倾向**：库默认也改为真 runner + yaml active_profile，避免「忘记注入又假成功」；单测必须显式 Fake（改测试，换安全默认）。

#### D5. Planner

| 模式 | 行为 |
|------|------|
| deterministic（默认 env 未开） | 保持 `WorkflowPlanner()`，不调 LLM |
| prompt_guided / llm / real | 使用 **与主会话相同 profile** 的 client（Ink 有 agent 时）；无 agent 时用 yaml active_profile 或 env **profile 名**（不是 mykey 键） |

Env 迁移：

| 旧 | 新 |
|----|-----|
| `GA_REAL_API_CONFIG=native_oai_config` | `GA_WORKFLOW_LLM_PROFILE=grok`（profile 名） |
| `GA_WORKFLOW_PLANNER_CONFIG` | 同上或弃用，优先 profile |
| `GA_REAL_API_EXPECTED_MODEL` | 仍可作断言；默认改为跟 yaml 解析结果，不写死 gpt-5.5 |

兼容：若仍传入旧 mykey 键且能在 mykey 找到，可 **过渡期** 警告后走 resolve（可选）；**默认文档与测试不再依赖**。

#### D6. 命名

`NativeGPTChildAgentRunner` 名有历史包袱（并非只 GPT）。可选：

- 短期：保留类名，改构造语义（少 churn）
- 中期：别名 `WorkflowChildAgentRunner = NativeGPTChildAgentRunner` 并文档化

方案默认 **短期保留类名**，避免无意义大重命名阻塞接线。

---

## 4. 模块级修改清单

### 4.1 新建：`workflow_llm.py`（建议）

职责单一，**不** import Ink / `ga.py` 前端：

```text
WorkflowLlmBinding
  profile_name: str
  model_id: str
  source: "agent" | "profile" | "explicit"

binding_from_agent(agent) -> WorkflowLlmBinding
binding_from_profile(profile_name, config?=None) -> WorkflowLlmBinding
binding_from_env() -> WorkflowLlmBinding   # GA_WORKFLOW_LLM_PROFILE / active_profile

make_tool_client(binding) -> NativeToolClient   # build_client(yaml, profile)
make_session(binding) -> Session                # build_session，供 planner 无 tools

# 可选：describe_binding() 供 journal / progress 元数据
```

规则：

- `binding_from_agent`：读 `agent.get_llm_name()` / `backend.name` / `backend.model`；profile 优先 `backend.name`（与 `/model` 列表一致）。
- `make_tool_client`：**每次调用新建** client，清空 history 语义由新实例保证。
- 找不到 profile → 明确错误（含可用 profile 列表），**禁止**静默 Fake。

### 4.2 `workflow_child_agent.py`

**改：**

1. `NativeGPTChildAgentRunner.__init__` 增加：
   - `binding_provider: Callable[[], WorkflowLlmBinding] | None = None`
   - `profile_name: str | None = None`（固定 profile，测试/脚本用）
   - 保留 `client_factory` / `session_factory`（最高优先，单测）
2. `_new_executable` 优先级：
   ```text
   client_factory / session_factory
     → binding_provider() → make_tool_client / make_session
     → profile_name → make_*
     → binding_from_env()
     → 不再调用 resolve_client("native_oai_config") 作为默认
   ```
3. transcript `metadata` 增加 `llmProfile` / `llmModel`（脱敏，无 key）。
4. 文档字符串标明：生产应由 bridge 注入 `binding_provider=lambda: binding_from_agent(agent)`。

**FakeChildAgentRunner**：保留；仅测试与显式注入。

### 4.3 `workflow_runtime.py`

**改：**

1. 默认 runner 策略（推荐实现）：
   ```python
   self.runner = runner if runner is not None else self._default_runner()
   ```
   `_default_runner()` → `NativeGPTChildAgentRunner(binding_provider=binding_from_env)`
   或要求 `runner` 必填——**更推荐有安全默认真 runner**，避免再假成功。
2. `run()` 开始时写 journal/progress：`llmBinding` 快照（profile/model）。
3. 构造参数可增 `llm_binding_provider`，在未传 `runner` 时用于默认 Native runner。

**注意**：全库凡 `WorkflowRuntime(store=...)` 无 runner 的**单测**都会变成「可能碰 yaml/真 client」。必须批量改为显式 `runner=FakeChildAgentRunner()`（见 §5）。

### 4.4 `frontends/ink_bridge.py`

**改 `_make_workflow_runtime`：**

```text
kwargs = {store, timeout?}
if workflow_runtime_factory: return factory(**kwargs)

binding_provider = lambda: binding_from_agent(self.agent)
runner = NativeGPTChildAgentRunner(binding_provider=binding_provider, enable_tools=True)
return WorkflowRuntime(store=..., runner=runner, timeout_seconds=..., ...)
```

**改 `_make_workflow_planner`：**

- env deterministic → 不变
- env prompt_guided → `NativeWorkflowPlannerClient` 改为基于 **agent 当前 profile** 的 client（`make_session(binding_from_agent(self.agent))`），而不是 `resolve_session(native_oai_config)`
- 允许 `workflow_planner_factory` 测试注入不变

**改 `_run_workflow_runtime` / approve / plan：** 确保异常信息在 profile 缺失时可读。

可选：workflow 开始 emit 一条 system/status：`workflow llm: profile/model=grok/grok-4.5`（无密钥）。

### 4.5 `workflow_planner.py`

**改：**

1. `resolve_session` 包装改为 `workflow_llm.make_session` / `binding_from_env`。
2. `NativeWorkflowPlannerClient(config_name=...)` → 演进为：
   - `NativeWorkflowPlannerClient(profile_name=...)` 或
   - `NativeWorkflowPlannerClient(binding_provider=...)`
3. `build_workflow_planner_from_env`：
   - `GA_WORKFLOW_LLM_PROFILE` 优先于旧 `GA_*_CONFIG`
   - 旧键若仍存在：DeprecationWarning + 尝试 mykey **仅过渡**（可开关 `GA_WORKFLOW_ALLOW_LEGACY_MYKEY=1`，默认关）

### 4.6 `workflow_scheduler.py` / store / progress

- cache key 已有 tool/mcp context hash；**建议**把 `llmProfile`+`model` 纳入 cache 分区（避免 `/model` 切换后错误命中旧 child 缓存）。
- `workflow-progress.json` agent 摘要增加 `llmProfile` / `llmModel`（可选但利于排障）。

### 4.7 不改或少改

- `workflow_js_worker.js`：与 LLM 无关
- `workflow_permissions.py`：正交
- `workflow_models` 状态机：仅 metadata 扩展
- 主会话 `load_llm_sessions`：不回退 mykey

---

## 5. 测试修改矩阵

### 5.1 原则

| 类型 | 策略 |
|------|------|
| 纯编排 / Fake | **显式** `FakeChildAgentRunner`；断言不碰网络 |
| Native 单测 | **显式** `client_factory` / `session_factory` stub（与现在多数 test_workflow_child_agent 一致） |
| 默认接线 | **新增**测试：bridge 未注入 factory 时 runner **不是** Fake；binding 来自 agent 当前 profile |
| `/model` 跟随 | **新增**：agent 切换 profile 后，下一个 job 的 factory 收到新 profile |
| opt-in 真 E2E | profile 名 + 期望 model 从 yaml/env 读；默认示例改 `grok` / `grok-4.5`（本机约定） |
| 禁止 | 新增代码路径默认 `native_oai_config`；新测试依赖 mykey 键名 |

### 5.2 必改文件（按优先级）

#### A. 生产接线回归（新建）

- `tests/test_workflow_llm.py`（新）
  - binding_from_agent / from_profile / make_tool_client 用临时 yaml fixture
  - 未知 profile 报错
- `tests/test_ink_bridge.py` 增补：
  - `_make_workflow_runtime` 默认 runner 类型为 Native（可 patch `make_tool_client` 避免真连）
  - agent `backend.name="grok"` 时 binding.profile == `grok`

#### B. 默认 runner 变更引起的连锁

凡构造 `WorkflowRuntime(...)` **未传 runner** 的测试，改为显式 Fake，否则会走真默认：

- 重点扫：`tests/test_workflow_runtime.py`、`tests/test_workflow_integration.py`、`tests/test_workflow_scheduler.py`（若有）、`tests/test_ink_bridge.py` 内部 runtime
- 策略：全局检索 `WorkflowRuntime(`，无 `runner=` 则补上

#### C. child / planner 单测

- `tests/test_workflow_child_agent.py`
  - 去掉「默认 config_name == native_oai_config」硬断言
  - 增加：无 factory 时走 `profile_name` / binding_provider
  - 保留 client_factory stub 用例
- `tests/test_workflow_prompt_guided_planner.py`
  - patch 目标从 `workflow_planner.resolve_session` 改为 `workflow_llm.make_session` 或 client 接口
  - env 测试：`GA_WORKFLOW_LLM_PROFILE` 替代 `GA_REAL_API_CONFIG=native_oai_config`

#### D. opt-in 真实 / 半真实脚本

统一头：

```python
PROFILE = os.environ.get("GA_WORKFLOW_LLM_PROFILE") or os.environ.get("GA_REAL_API_PROFILE") or "grok"
# 期望 model 从 yaml resolve，或 env 覆盖
```

涉及文件（至少）：

- `tests/test_workflow_real_llm_integration.py`
- `tests/real_complex_workflow_mcp_skill_coding_e2e.py`
- `tests/real_workflow_slice7_schema_fallback_e2e.py`
- `tests/p8_real_api_e2e.py` 及 stress/stability
- `tests/prompt_guided_planner_real_e2e.py` / `prompt_guided_planner_real_child_e2e.py`
- `tests/workflow_planner_real_e2e.py`
- `tests/optional_skill_real_e2e.py`
- `tests/real_ink_bridge_workflow_detail_smoke.py` 等 real_ink_*

门闩保持：`GA_RUN_REAL_API_E2E=1` 等；**不**在 CI 默认跑。

#### E. 文档测试说明

- `docs/GA_workflow_complex_e2e_verification.md`
- `docs/GA_workflow_user_guide.md`
- `docs/P8-e2e-*.md` 中 config 名段落

改为 profile 语义 + 「subagent 跟随主会话 /model」。

### 5.3 验收测试清单（实现后必须跑）

```text
# L0 无网
python -m unittest discover -s tests -p "test_workflow*.py"
python -m unittest tests.test_ink_bridge tests.test_workflow_llm
python -m unittest discover -s tests -p "test_*.py"   # 全量防回归

# Ink TS（若 bridge 协议字段有增）
cd frontends/ink-ui && npm run test && npm run typecheck

# L2 opt-in（本机，跟 /model 或 profile=grok）
# 先 ga /model 到 grok，或 GA_WORKFLOW_LLM_PROFILE=grok
GA_RUN_REAL_API_E2E=1 GA_WORKFLOW_LLM_PROFILE=grok \
  python tests/real_complex_workflow_mcp_skill_coding_e2e.py
```

断言要点：

1. 默认 bridge runtime **不是** Fake
2. child transcript metadata 含 profile/model 且与 agent 当前一致
3. `/model` 切换后新 job 元数据变化
4. Fake 显式注入时行为与今日单测一致
5. 全量 unittest 仍绿

---

## 6. 分阶段实现计划

### 阶段 0：冻结行为说明（0.5d）

- 本 MD 评审确认 D1–D6
- 在 `docs/GA_workflow_defect_optimization_plan.md` 或 roadmap 顶部加 **「已知：默认 Fake + mykey child」** 警告（实现完成前）

### 阶段 1：`workflow_llm` + child 解析切换（1d）

1. 新增 `workflow_llm.py` + `tests/test_workflow_llm.py`
2. 改 `NativeGPTChildAgentRunner._new_executable` 走 binding
3. 单测 child：stub 仍绿；删除对 `native_oai_config` 默认依赖
4. **暂不**改 Runtime 默认（避免一次炸全库）

### 阶段 2：Runtime / Bridge 默认真 runner（1d）

1. bridge `_make_workflow_runtime` 注入 agent binding_provider + Native runner
2. Runtime 默认策略落地
3. 全库 `WorkflowRuntime(` 无 runner 的测试补 Fake
4. 新增 bridge 接线回归测试
5. cache key 纳入 profile/model（若本阶段顺手）

### 阶段 3：Planner 对齐（0.5–1d）

1. `NativeWorkflowPlannerClient` / `build_workflow_planner_from_env` 改 profile
2. bridge planner 使用 agent 当前 binding
3. 更新 planner 单测与 env 测试

### 阶段 4：真实 E2E / 文档（0.5–1d）

1. 批量改 opt-in 脚本默认 profile
2. 更新 user guide / complex e2e 文档
3. 本机用 **grok** 跑通至少 1 条 complex 或 planner+child 冒烟（opt-in）
4. 进度回写 defect plan：W-LLM-1/2/3 关闭条件

### 阶段 5（可选后续）：未完成 workflow 产品能力

在 **新 LLM 接入稳定之后** 再做（避免两套配置上继续堆功能）：

| 项 | 说明 |
|----|------|
| 受控 Test Gate 一等化 | defect plan 阶段四 |
| TDD template / repair loop | 阶段五 |
| 更完整真实 API 矩阵 | 阶段九，统一 `GA_WORKFLOW_LLM_PROFILE` |
| 默认 planner 是否改 prompt_guided | 产品另议；技术上已能跟 `/model` |

**原则：未实现功能的新代码只允许 yaml/binding API，禁止再引入 resolve_session(mykey)。**

---

## 7. API 草图（实现时以代码为准）

### 7.1 Binding

```python
# workflow_llm.py（示意）
@dataclass(frozen=True)
class WorkflowLlmBinding:
    profile_name: str
    model_id: str = ""
    source: str = "profile"  # agent | profile | env

def binding_from_agent(agent) -> WorkflowLlmBinding:
    client = getattr(agent, "llmclient", None)
    backend = getattr(client, "backend", None)
    profile = getattr(backend, "name", None) or "default"
    model = getattr(backend, "model", None) or ""
    return WorkflowLlmBinding(profile_name=str(profile), model_id=str(model), source="agent")

def make_tool_client(binding: WorkflowLlmBinding):
    from llm_client import build_client
    from llm_config import load_llm_config, find_llm_config
    cfg = load_llm_config(find_llm_config())
    return build_client(cfg, binding.profile_name)
```

### 7.2 Bridge

```python
def _make_workflow_runtime(self, *, timeout_seconds=None):
    kwargs = {"store": self.workflow_store}
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = float(timeout_seconds)
    if self.workflow_runtime_factory is not None:
        return self.workflow_runtime_factory(**kwargs)
    from workflow_child_agent import NativeGPTChildAgentRunner
    from workflow_llm import binding_from_agent
    from workflow_runtime import WorkflowRuntime
    agent = self.agent
    runner = NativeGPTChildAgentRunner(
        binding_provider=lambda: binding_from_agent(agent),
        enable_tools=True,
    )
    return WorkflowRuntime(runner=runner, **kwargs)
```

### 7.3 `/model` 跟随（时序）

```text
T0 用户 /model grok     → agent.llm_no 指向 grok/grok-4.5
T1 workflow_approve     → runtime 启动
T2 job1 start           → binding_provider() → grok
T3 用户 /model glm      → 仅主会话切换
T4 job2 start           → binding_provider() → glm
T5 job1 仍在跑          → 仍用启动时已构造的 client（不热切）
```

---

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 默认真 runner 导致单测触网/变慢 | 全量显式 Fake；CI 无 key |
| `build_client` 每 job 成本 | 可接受；必要时按 profile 进程内缓存 **Session 类模板** 但仍新 history |
| mixin profile 作 child | 允许；与主会话一致；注意 MixinSession 行为 |
| yaml 缺失 | 与主会话同样失败；错误打到 workflow_failed，勿假成功 |
| 旧脚本 `GA_REAL_API_CONFIG` | 文档迁移；可选兼容一层后删除 |
| cache 跨模型命中 | cache key 加 profile+model |
| 与 permission 交互 | child 仍只挂 workflow_permission_policy；不挂主会话 ask runtime |

---

## 9. 明确非目标（本方案不做）

- 回退主会话到 mykey 分发
- workflow child 阻塞人审
- 档位持久化
- 强制所有 workflow 使用固定 grok（只约定 live 验证偏好；运行时跟 `/model`）
- 一次重写整个 defect plan 未完成产品功能（仅约束其 LLM 接入方式）

---

## 10. 完成定义（DoD）

1. 生产 Ink 路径：`_make_workflow_runtime().runner` 为真 child，且 `_new_executable` **不**调用 `resolve_client/session(mykey)`。
2. Subagent 使用的 profile/model 与 `agent.get_llm_name()` / `get_llm_name(model=True)` 一致（job start 快照）。
3. `/model` 切换后新 job 跟随；有单测。
4. `tests/test_workflow*.py` + `test_ink_bridge` + 全量 `test_*.py` 绿。
5. 默认路径不再出现「Fake 假成功」 unless 测试显式注入。
6. 文档与 opt-in E2E 使用 **profile** 语义；本地方便用 `grok` 验证。
7. 新增/未完成 workflow 功能禁止引入新的 mykey resolve 默认路径。

---

## 11. 建议提交切分（实现期）

```text
1. feat(workflow-llm): 新增 workflow_llm binding + child 解析
2. test(workflow-llm): binding/agent 跟随与未知 profile
3. feat(workflow): bridge/runtime 默认真 runner + 单测显式 Fake
4. feat(workflow-planner): planner 走 yaml profile
5. test(workflow): 更新 real e2e 默认 profile 与文档
```

中文 Conventional Commit 前缀保持仓库习惯。

---

## 12. 总结

| 维度 | 现在 | 目标 |
|------|------|------|
| 主会话 | llm.yaml profile ✅ | 不变 |
| Workflow child 默认 | Fake ❌ | Native + 跟 `/model` |
| Workflow child 配置 | mykey `native_oai_*` ❌ | `build_client(yaml, profile)` |
| Planner 真模式 | mykey ❌ | 同 profile binding |
| 测试 | 大量旧 config 名 | Fake 显式；E2E 用 profile |
| 未完成功能 | 易继续写旧接入 | 一律新 binding |

**核心修复不是「再写一个 mykey 键」，而是：workflow 接入层并入主会话已完成的 yaml 客户端构造，并在产品默认路径挂上真 subagent，且模型选择键绑定 `GenericAgent` 当前 `/model`。**

---

*本文为修改方案，不替代实现 PR。实现以本文件 §2 产品规则与 §10 DoD 为准。*

---

## 13. 实现进度（2026-07-22，本分支落地）

| 项 | 状态 |
|----|------|
| `workflow_llm.py` binding / make_tool_client / make_session | ✅ |
| `NativeGPTChildAgentRunner` 默认走 yaml binding，不再 resolve mykey | ✅ |
| `WorkflowRuntime` 默认真 runner；单测显式 Fake | ✅ |
| `ink_bridge._make_workflow_runtime` 注入 agent `/model` binding | ✅ |
| planner `NativeWorkflowPlannerClient` + env profile | ✅ |
| 单测 `test_workflow_llm` + 相关修正 | ✅ |
| 全量 `test_*.py` 640 OK | ✅ |
| 真 grok child：`GA_RUN_REAL_LLM_TESTS=1` + profile=grok | ✅ |
| 真 grok `WorkflowRuntime` live：`LIVE_CHILD_OK` | ✅ |
| 真 grok prompt-guided planner e2e | ✅（runtime 场景用 Fake child 跑 script；planner LLM 真） |
| 全部 real e2e 脚本统一 profile（p8 等） | ⏳ 部分（complex + planner real + real_llm_integration 已改；p8 系列可同模式跟进） |
| cache key 纳入 llmProfile/model | ⏳ 未做（后续小 PR） |

### 本机 live 命令备忘

```bash
# child 单测
GA_RUN_REAL_LLM_TESTS=1 GA_WORKFLOW_LLM_PROFILE=grok \
  python -m unittest tests.test_workflow_real_llm_integration -v

# planner 真 LLM（child 仍 Fake 跑 script）
GA_RUN_REAL_PROMPT_PLANNER_E2E=1 GA_WORKFLOW_LLM_PROFILE=grok \
  GA_REAL_API_EXPECTED_MODEL=grok-4.5 GA_REAL_API_EXPECTED_NAME=grok \
  python tests/prompt_guided_planner_real_e2e.py
```

**产品默认路径：** Ink bridge 创建的 `WorkflowRuntime` 使用 `NativeGPTChildAgentRunner(binding_provider=binding_from_agent)`，subagent 跟主会话当前 `/model`。
