# Per-Model Reasoning Effort Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 GA 的思考等级从全局硬编码迁移为模型能力元数据，并在 Ink UI 中实现 Codex 风格的 `/model → 模型 → 思考等级` 二级选择，使 `gpt-6-luna` 和 `deepseek-v4.1-flash` 可以各自使用真实支持的等级。

**Architecture:** 配置层为每个模型声明 `reasoning_efforts`、`default_reasoning_effort`，profile 只覆盖当前 `reasoning_effort`；运行时在模型切换时确定性地保留或回退等级；请求层只发送目标模型声明支持的等级。Ink bridge 暴露模型能力和当前等级，前端把模型选择与思考等级选择建模为两个连续面板。旧的 `thinking` 配置继续兼容，但不再作为新模型能力列表的来源。

**Tech Stack:** Python 3.10+、Pydantic、PyYAML、OpenAI Chat Completions/Responses、TypeScript、Node test runner、Ink。

---

## Scope and non-goals

本轮只实现 `/model` 及其二级思考等级选择，不实现 `/mode`。`/mode` 可以在后续作为当前模型思考等级的快捷入口，但不得在本轮引入 Codex collaboration/Plan mode 的语义。

本轮必须满足：

- `gpt-6-luna` 支持 `none/minimal/low/medium/high/xhigh/max`；官网和当前 API 均未确认 `ultra`，因此不加入；
- `deepseek-v4.1-flash` 只显示配置中确认可用的等级；真实 API 已确认 `none`，因此配置为 `none/low/high/max`；
- 模型切换后，当前等级若仍受新模型支持则保留，否则回退到新模型默认等级，再回退到支持列表第一个等级；
- Responses API 和 Chat Completions 都传递选中的等级；
- 不支持的等级在宿主侧被拒绝，不依赖模型提示词兜底；
- 旧模型和旧 `thinking` 配置仍可启动。

### Legacy `thinking` policy

本轮不删除 `thinking` 字段，而是将它标记为 deprecated 兼容入口：

- OpenAI Chat/Responses：`thinking: off` 映射为 `reasoning_effort: none`；`low/medium/high/max/xhigh/ultra` 均按原值传递，`max` 与 `xhigh` 不互相映射；具体模型支持哪些等级由 `reasoning_efforts` 声明决定。
- Anthropic：`thinking: off` 映射为 `thinking_type: disabled`；其它等级使用 `thinking_type: adaptive`，支持的 `output_config.effort` 值按原值传递，`max` 与 `xhigh` 保持独立。
- 若同时存在 `reasoning_effort` 与 `thinking`，显式 `reasoning_effort` 优先，这是现有行为，迁移后继续保持。
- 新增或迁移的 `gpt-6-luna`、`deepseek-v4.1-flash` 配置不再写 `thinking`，直接使用 `reasoning_efforts`、`default_reasoning_effort` 和 `reasoning_effort`。
- 只有在未来确认所有本地配置和 Anthropic 适配器都完成迁移后，才另起版本移除 `thinking`；本轮不能删除，因为现有 Claude 配置、profile 和回归测试仍依赖其语义。

## File map

- Modify: `llm_config.py` — 模型能力字段、profile 覆盖、旧配置兼容和解析后的运行态数据。
- Modify: `llmcore.py` — 扩展 reasoning effort 合法值和请求层透传。
- Modify: `agentmain.py` — 模型描述、当前等级读取、模型切换回退、显式等级切换。
- Modify: `frontends/ink_bridge.py` — 扩展 model status/switch bridge 协议和宿主校验。
- Modify: `frontends/ink-ui/src/protocol.ts` — 增加模型 reasoning 能力和二级选择命令类型。
- Modify: `frontends/ink-ui/src/modelPanel.ts` — 二级选择状态、等级移动和模型切换回退展示所需的纯函数。
- Modify: `frontends/ink-ui/src/App.tsx` — 模型面板与 reasoning 面板的焦点、返回、确认和状态更新。
- Modify: `frontends/ink-ui/src/slashCommands.ts` — 更新 `/model` 与 `/llm` 描述；不增加 `/mode`。
- Test: `tests/test_llm_config.py` — 配置字段、旧字段兼容、模型能力和回退。
- Test: `tests/test_llm_profiles.py` — profile 当前等级覆盖。
- Test: `tests/test_llmcore_history_content.py` 或新增 `tests/test_llm_reasoning_effort.py` — Chat/Responses payload。
- Test: `tests/test_agentmain_model_selection.py` — 模型和等级切换。
- Test: `frontends/ink-ui/src/modelPanel.test.ts` — 二级面板纯函数。
- Test: `frontends/ink-ui/src/inputController.test.ts`、`frontends/ink-ui/src/App.test.ts` — `/model` 和键盘流程。
- Modify: `llm.yaml.example` — 提供不含密钥的 per-model 配置示例。
- Create: `docs/20261004-reasoning-effort-model-migration.md` — 本迁移记录；实施完成后追加真实验证结果，不写入 key 或 Authorization。

### Task 1: 建立模型级 reasoning 能力数据模型

**Files:**
- Modify: `llm_config.py:43-90, 160-300, 311-390`
- Test: `tests/test_llm_config.py`
- Test: `tests/test_llm_profiles.py`

- [ ] **Step 1: 写失败测试，锁定模型级配置语义**

在 `tests/test_llm_config.py` 增加以下行为测试（使用内存 YAML 或临时文件，不读取本机真实密钥）：

```python
def test_model_reasoning_capabilities_are_model_specific(tmp_path):
    path = tmp_path / "llm.yaml"
    path.write_text(
        """
providers:
  luna: {wire_api: openai_responses, base_url: https://example.test/v1, api_key: x}
  deepseek: {wire_api: openai_responses, base_url: https://example.test/v1, api_key: x}
models:
  gpt-6-luna:
    provider: luna
    reasoning_efforts: [none, minimal, low, medium, high, xhigh, max]
    default_reasoning_effort: medium
    reasoning_effort: medium
  deepseek-v4.1-flash:
    provider: deepseek
    reasoning_efforts: [none, low, high, max]
    default_reasoning_effort: high
    reasoning_effort: high
profiles:
  luna: {model: gpt-6-luna}
  deepseek: {model: deepseek-v4.1-flash}
""",
        encoding="utf-8",
    )
    config = load_llm_config(str(path))
    luna = config.resolve("luna")
    deepseek = config.resolve("deepseek")
    assert luna.supported_reasoning_efforts == [
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ]
    assert deepseek.supported_reasoning_efforts == ["low", "high", "max"]
    assert deepseek.default_reasoning_effort == "high"
```

```python
def test_legacy_model_without_reasoning_efforts_still_loads(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text(
        """
providers:
  p: {wire_api: openai_chat, base_url: https://example.test/v1, api_key: x}
models:
  legacy:
    provider: p
    thinking: high
profiles:
  default: {model: legacy}
active_profile: default
""",
        encoding="utf-8",
    )
    resolved = load_llm_config(str(path)).resolve()
    assert resolved.supported_reasoning_efforts is None
    assert resolved.reasoning_effort == "high"
```

测试必须明确断言：没有 `reasoning_efforts` 的旧模型不会自动获得 DeepSeek 或 Luna 的完整等级列表。

- [ ] **Step 2: 运行失败测试**

运行：

```bash
python -m unittest tests.test_llm_config tests.test_llm_profiles -v
```

预期：新字段尚不存在，测试失败。

- [ ] **Step 3: 实现配置字段和归一化方法**

在 `ModelCfg` 增加：

```python
reasoning_efforts: Optional[list[str]] = None
default_reasoning_effort: Optional[str] = None
```

在 `ProfileCfg` 增加：

```python
reasoning_effort: Optional[str] = None
```

增加统一校验函数：

```python
def _validate_reasoning_efforts(
    efforts: Optional[list[str]],
    default: Optional[str],
    current: Optional[str],
) -> None:
    if efforts is not None:
        if not efforts:
            raise ValueError("reasoning_efforts 不能是空列表；未知能力请省略该字段")
        if any(not isinstance(item, str) or not item.strip() for item in efforts):
            raise ValueError("reasoning_efforts 中每一项必须是非空字符串")
        if len(set(efforts)) != len(efforts):
            raise ValueError("reasoning_efforts 不能包含重复等级")
        if default is not None and default not in efforts:
            raise ValueError("default_reasoning_effort 必须位于 reasoning_efforts 中")
        if current is not None and current not in efforts:
            raise ValueError("reasoning_effort 必须位于 reasoning_efforts 中")
```

`ResolvedModel` 暴露：

```python
supported_reasoning_efforts: Optional[list[str]]
default_reasoning_effort: Optional[str]
reasoning_effort: Optional[str]
```

`reasoning_efforts is None` 表示“能力未声明”，不是“支持所有等级”。这样旧模型可以继续运行，但 UI 不会显示虚假的等级选项。

保留现有 `thinking` 到 wire 字段的兼容翻译，并在字段注释中标记 deprecated；当显式 `reasoning_effort` 存在时，显式值优先。不要把旧的 `THINKING_LEVELS` 用来验证新的 per-model 能力列表。新增测试必须覆盖 `thinking: off` 的 OpenAI 和 Anthropic 映射，以及显式 `reasoning_effort` 覆盖旧字段的行为。

- [ ] **Step 4: 运行配置测试**

运行：

```bash
python -m unittest tests.test_llm_config tests.test_llm_profiles -v
```

预期：新增测试和原有配置测试全部通过。

- [ ] **Step 5: 提交配置模型变更**

```bash
git add llm_config.py tests/test_llm_config.py tests/test_llm_profiles.py
git commit -m "feat(llm): add per-model reasoning capabilities"
```

### Task 2: 扩展请求层并保持协议兼容

**Files:**
- Modify: `llmcore.py:590-620, 723-760`
- Test: `tests/test_llm_reasoning_effort.py`

- [ ] **Step 1: 写 Chat/Responses payload 测试**

使用 fake HTTP transport 捕获 payload，分别断言：

```python
assert responses_payload["reasoning"] == {"effort": "max"}
assert chat_payload["reasoning_effort"] == "max"
```

同时测试 `none`，确认它被原样发送而不是被当作空值丢弃。

- [ ] **Step 2: 扩展 BaseSession 的允许值**

将 `BaseSession` 的 reasoning effort 允许集合扩展为：

```python
{"none", "minimal", "low", "medium", "high", "xhigh", "max"}
```

如果配置层已经确认目标模型的自定义等级，则运行时不得因为固定枚举再次拒绝；对未知自定义字符串保留明确的模型能力校验，由 agent 层负责，而不是在 wire 层硬编码拒绝。

- [ ] **Step 3: 运行协议测试**

```bash
python -m unittest tests.test_llm_reasoning_effort -v
```

预期：Chat Completions 和 Responses 两条路径都通过，`none`、`max` 均保持原值；wire 层仍允许未来 provider 自定义等级，但 Luna 配置不宣传未确认的 `ultra`。

- [ ] **Step 4: 提交请求层变更**

```bash
git add llmcore.py tests/test_llm_reasoning_effort.py
git commit -m "feat(llm): forward extended reasoning efforts"
```

### Task 3: 实现宿主侧模型切换和等级回退

**Files:**
- Modify: `agentmain.py:525-650`
- Test: `tests/test_agentmain_model_selection.py`

- [ ] **Step 1: 写失败测试**

覆盖以下确定性规则：

```python
def test_switch_model_preserves_supported_effort():
    assert resolve_reasoning_effort_for_model("low", ["low", "high"], "high") == "low"
def test_switch_model_falls_back_to_target_default():
    assert resolve_reasoning_effort_for_model("ultra", ["low", "high"], "high") == "high"

def test_switch_model_falls_back_to_first_supported_when_default_invalid():
    assert resolve_reasoning_effort_for_model("ultra", ["low", "high"], "medium") == "low"

def test_unknown_model_capabilities_do_not_invent_reasoning_choices():
    assert resolve_reasoning_effort_for_model("high", None, "medium") == "high"

def test_explicit_unsupported_effort_is_rejected_before_request():
    agent = make_fake_agent_with_reasoning_capabilities(["low", "high"])
    result = agent.select_reasoning_effort("ultra")
    assert result["ok"] is False
    assert result["code"] == "unsupported_reasoning_effort"
```

测试使用 fake clients，不访问真实 API。

- [ ] **Step 2: 增加运行态模型描述**

让 `Agent` 暴露结构化模型描述，而不是只返回 `(index, name, current)`：

```python
{
    "index": index,
    "name": name,
    "current": current,
    "reasoningEfforts": list(efforts or []),
    "reasoningEffortKnown": efforts is not None,
    "defaultReasoningEffort": default_effort,
    "reasoningEffort": current_effort,
}
```

保留旧 `list_llms()` 调用兼容性，新增 `list_llm_descriptors()` 供 Ink bridge 使用。

- [ ] **Step 3: 实现回退函数**

在 `agentmain.py` 增加纯函数，便于单测：

```python
def resolve_reasoning_effort_for_model(
    current: str | None,
    supported: list[str] | None,
    default: str | None,
) -> str | None:
    if supported is None:
        return current
    if current in supported:
        return current
    if default in supported:
        return default
    return supported[0] if supported else None
```

模型切换时必须先解析目标模型的能力，再重建/切换 session；禁止先把旧等级写入新 session 后再等待 API 报错。

- [ ] **Step 4: 增加显式等级切换入口**

实现：

```python
def select_reasoning_effort(self, effort: str) -> dict:
    normalized = str(effort or "").strip().lower()
    descriptor = self.current_llm_descriptor()
    supported = descriptor.get("reasoningEfforts")
    if descriptor.get("reasoningEffortKnown") is not True:
        return {"ok": False, "code": "reasoning_capabilities_unknown"}
    if normalized not in supported:
        return {"ok": False, "code": "unsupported_reasoning_effort"}
    self.llmclient.backend.reasoning_effort = normalized
    return {"ok": True, "effort": normalized}
```

规则：

- 当前模型能力未知时返回可解释错误：“当前模型未声明可选思考等级”；
- 当前模型不支持该等级时返回支持列表；
- 支持时更新当前 session 的 backend `reasoning_effort`，保留历史和工具状态；
- agent 正在运行时拒绝修改。

- [ ] **Step 5: 运行宿主测试**

```bash
python -m unittest tests.test_agentmain_model_selection -v
```

- [ ] **Step 6: 提交宿主切换变更**

```bash
git add agentmain.py tests/test_agentmain_model_selection.py
git commit -m "feat(agent): make reasoning effort model-aware"
```

### Task 4: 扩展 Ink bridge 与协议

**Files:**
- Modify: `frontends/ink_bridge.py:475-505, 1520-1540`
- Modify: `frontends/ink-ui/src/protocol.ts`
- Test: `frontends/ink-ui/src/inputController.test.ts`

- [ ] **Step 1: 扩展 bridge command 类型**

将 model switch 命令扩展为：

```ts
| {
    type: 'model_switch'
    selector: string
    reasoningEffort?: string
  }
| { type: 'reasoning_effort_switch'; effort: string }
```

`model_status` 中的每个 `ModelStatus` 增加：

```ts
reasoningEfforts: string[]
reasoningEffortKnown: boolean
defaultReasoningEffort?: string | null
reasoningEffort?: string | null
```

- [ ] **Step 2: 实现 bridge 宿主校验**

`model_switch` 支持可选的 `reasoningEffort`，由 `Agent.select_llm()` 和 `Agent.select_reasoning_effort()` 完成确定性验证；失败时只发 `model_switch_result`/`error`，不得修改当前 model。

`reasoning_effort_switch` 只修改当前模型的等级，并重新发送完整 `model_status`，避免 UI 使用过期能力列表。

- [ ] **Step 3: 测试 bridge 协议解析**

运行：

```bash
node --import tsx --test frontends/ink-ui/src/inputController.test.ts
```

预期：`/model` 和 `/llm` 仍能产生 model switch 命令，扩展字段可选且不会破坏旧调用。

- [ ] **Step 4: 提交 bridge 变更**

```bash
git add frontends/ink_bridge.py frontends/ink-ui/src/protocol.ts frontends/ink-ui/src/inputController.test.ts
git commit -m "feat(ink): expose model reasoning capabilities"
```

### Task 5: 实现 Codex 风格 `/model` 二级选择器

**Files:**
- Modify: `frontends/ink-ui/src/modelPanel.ts`
- Modify: `frontends/ink-ui/src/App.tsx`
- Modify: `frontends/ink-ui/src/slashCommands.ts`
- Test: `frontends/ink-ui/src/modelPanel.test.ts`
- Test: `frontends/ink-ui/src/App.test.ts`

- [ ] **Step 1: 写面板状态和移动逻辑测试**

面板状态至少包含：

```ts
type ReasoningPanelState = {
  modelIndex: number
  modelName: string
  efforts: string[]
  selected: number
  current?: string | null
}
```

测试要求：

- 只显示目标模型声明的等级；
- 当前等级正确定位；
- 上下键在 reasoning 面板边界停止，不循环；
- 空列表或能力未知时显示不可选提示；
- Escape 从 reasoning 面板返回 model 面板，而不是直接关闭整个 `/model` 流程。

- [ ] **Step 2: 实现模型确认后的二级面板**

现有模型面板确认某个模型时：

1. 如果目标模型声明多个等级，保留 model 面板状态并打开 reasoning 面板；
2. 如果目标模型只有一个等级，直接提交 model + 该等级；
3. 如果目标模型能力未知，直接切换模型但不伪造 reasoning 列表；
4. 当前模型切换到新模型时，初始高亮目标模型的 default 或宿主返回的回退等级。

reasoning 面板确认时发送：

```ts
{
  type: 'model_switch',
  selector: String(modelIndex),
  reasoningEffort: efforts[selected],
}
```

- [ ] **Step 3: 更新 Ink 布局高度和焦点路由**

确保 reasoning 面板的行数由实际等级数量决定；不得复用固定的 `models.length + 2`。焦点优先级保持：MCP/workflow/permission/approval/model/reasoning 面板依次处理，Escape 逐层返回。

- [ ] **Step 4: 更新 slash 命令说明**

保持：

```ts
{ name: '/model', description: 'Show and switch AI models and reasoning effort' }
{ name: '/llm', description: 'Alias for /model' }
```

不增加 `/mode`。

- [ ] **Step 5: 运行 Ink 单测**

```bash
node --import tsx --test frontends/ink-ui/src/modelPanel.test.ts frontends/ink-ui/src/inputController.test.ts frontends/ink-ui/src/slashCommands.test.ts
```

再运行完整 Ink 测试：

```bash
node --import tsx --test frontends/ink-ui/src/*.test.ts
```

- [ ] **Step 6: 提交 Ink 二级选择器**

```bash
git add frontends/ink-ui/src/modelPanel.ts frontends/ink-ui/src/App.tsx frontends/ink-ui/src/slashCommands.ts frontends/ink-ui/src/modelPanel.test.ts frontends/ink-ui/src/App.test.ts
git commit -m "feat(ink): add model reasoning effort picker"
```

### Task 6: 更新示例配置并完成两模型迁移

**Files:**
- Modify: `llm.yaml.example`
- Modify: `docs/20261004-reasoning-effort-model-migration.md`
- Local-only: `llm.yaml`（被 `.gitignore` 忽略，不提交）

- [ ] **Step 1: 更新示例配置**

在 `llm.yaml.example` 写入不含 key 的示例：

```yaml
models:
  gpt-6-luna:
    provider: xem-gpt6-luna-responses
    api_model: gpt-6-luna
    reasoning_efforts: [none, minimal, low, medium, high, xhigh, max]
    default_reasoning_effort: medium
    reasoning_effort: medium

  deepseek-v4.1-flash:
    provider: xem-deepseek-v41-flash-responses
    api_model: deepseek-v4.1-flash
    reasoning_efforts: [none, low, high, max]
    default_reasoning_effort: high
    reasoning_effort: high
```

示例中不得再写 `thinking: off`。注释记录 DeepSeek 的 `none` 已通过真实 API 验证；能力列表仍按模型独立声明，不得因为 Luna 支持某个等级就复用到 DeepSeek。旧配置仍由兼容层解析，但迁移后的新配置只使用显式 reasoning 字段。

- [ ] **Step 2: 更新本机忽略配置**

在本机 `llm.yaml` 中为两个模型加入同样的能力字段；保留现有 endpoint 和 key，不把文件加入 Git。

- [ ] **Step 3: 验证配置解析**

```bash
python -c "from llm_config import load_llm_config; c=load_llm_config('llm.yaml'); print(c.resolve('default').model.api_model)"
```

预期输出：`gpt-6-luna`。

### Task 7: 完成真实 API 和 GA Ink E2E 验证

**Files:**
- Modify: `docs/20261004-reasoning-effort-model-migration.md`
- Create: `docs/20261004-reasoning-effort-model-migration-e2e.md` only if the project’s existing test-record convention requires a separate report.

- [ ] **Step 1: 验证 gpt-6-luna API**

使用本机 `llm.yaml`，不得在命令行、日志或文档中打印 key。分别通过 Responses API 测试：

```text
none
medium
max
```

每次请求只使用一个短开发问题，记录 HTTP 成功/失败、实际发送的 effort（脱敏）和响应是否正常。

- [ ] **Step 2: 验证 deepseek-v4.1-flash API**

分别测试：

```text
low
high
max
```

额外单独测试 `none`；本轮真实流式和非流式请求均成功，因此 UI 显示该等级。若未来 API 拒绝，必须在报告中记录并由配置移除，不得静默降级为 `low`。

- [ ] **Step 3: 验证 `/model` 切换流程**

在 GA Ink 中单独执行以下序列：

```text
/model
选择 gpt-6-luna
选择 max
发送一个短问题
/model
选择 deepseek-v4.1-flash
确认等级自动回退到 high（或本机配置的默认等级）
再选择 low
发送一个短问题
```

再反向执行一次，确认从 DeepSeek 切回 Luna 后不会把 `low` 错误地当作 Luna 的默认值，且可以继续选择 `max`。

- [ ] **Step 4: 验证宿主拒绝非法等级**

通过 bridge 或测试命令尝试给 DeepSeek 设置 `ultra`，确认返回明确错误并保持当前模型/等级不变。

- [ ] **Step 5: 运行全量回归**

```bash
python -m unittest discover -s tests -v
node --import tsx --test frontends/ink-ui/src/*.test.ts
```

- [ ] **Step 6: 记录结果并检查敏感信息**

文档只记录模型名、等级、耗时、状态码和脱敏错误摘要；执行：

```bash
git diff --check
git status --short
rg -n "sk-[A-Za-z0-9]|Authorization:|Bearer " docs llm.yaml.example
```

预期：仓库文档和示例中没有真实 API key 或 Authorization header。

## Self-review checklist

- [ ] `gpt-6-luna` 的 7 个已确认等级有配置、请求层、宿主校验、Ink 面板和真实 E2E 覆盖。
- [ ] `deepseek-v4.1-flash` 的等级列表独立配置，未默认继承 Luna 的 7 档。
- [ ] `none` 是否可用由真实 API 验证决定，不由全局枚举推断。
- [ ] 模型切换后的回退逻辑是宿主确定性执行，不依赖 LLM 提示词。
- [ ] Responses 和 Chat Completions 都覆盖了 `none/max` 或目标模型实际支持的等级；未确认的 `ultra` 不出现在 Luna 配置中。
- [ ] `/mode` 没有混入本轮实现；Codex 的 Plan/collaboration mode 语义未被误改。
- [ ] 旧 `thinking` 配置仍可加载，旧模型没有被强行伪造成支持所有等级。
- [ ] 新迁移的 Luna/DeepSeek 配置不再依赖 `thinking`；`thinking` 仅作为 deprecated 兼容入口保留。
- [ ] 计划中没有真实密钥、未定义的文件路径或未覆盖的核心需求。
