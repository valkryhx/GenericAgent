# Runtime Model Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let GA switch LLM profiles after startup through `/model`, without editing comments in `mykey.py` or restarting the Ink UI.

**Architecture:** Keep the current `native_oai_config` path compatible, then add an optional profile list that expands into normal GA LLM sessions. `GenericAgent` remains the source of truth for active model selection; TUI and Ink UI call the same runtime switching API. The Ink frontend gets a Claude Code style `/model` command: no args opens a picker, args switch directly, and `/model ?` shows the current model.

**Tech Stack:** Python 3 `unittest`, existing `agentmain.py` and `llmcore.py`; React/Ink TypeScript UI with Node test runner.

---

## Reference Notes

- GA already has runtime model switching through `GenericAgent.next_llm()` and `GenericAgent.list_llms()`.
- `tuiapp.py` and `tuiapp_v2.py` already expose `/llm`; Ink UI does not.
- Claude Code `/model` is a local immediate command:
  - `/model` opens a picker.
  - `/model <modelName>` switches directly.
  - `/model ?` or info args show the current model.
  - The command updates runtime app state first; persistence is separate and should not be required for GA v1.
- GA should not rewrite `mykey.py` during switching. The runtime choice should be in memory for this GA process.
- `mykey.py` can contain real secrets. Tests must use fake modules or monkeypatches and must not import the user's real secrets.

## File Structure

- Modify `llmcore.py`
  - Add profile expansion helpers near `_load_mykeys()` / `reload_mykeys()`.
  - Preserve existing public config variables unchanged.
- Modify `agentmain.py`
  - Add selector-based model APIs around current `next_llm()` implementation.
  - Keep `next_llm()` as compatibility wrapper.
- Modify `frontends/tuiapp.py`
  - Add `/model` alias to existing `/llm` command.
- Modify `frontends/tuiapp_v2.py`
  - Add `/model` alias to existing `/llm` command and command help.
- Modify `frontends/ink_bridge.py`
  - Add JSONL commands for model status and model switching.
- Modify `frontends/ink-ui/src/protocol.ts`
  - Add model command and event types.
- Modify `frontends/ink-ui/src/inputController.ts`
  - Parse `/model`, `/model <selector>`, `/llm`, `/llm <selector>`.
- Create `frontends/ink-ui/src/modelPanel.ts`
  - Pure helper for model list rendering state and selector labels.
- Modify `frontends/ink-ui/src/App.tsx`
  - Add a model picker panel similar to resume/MCP panels.
- Modify `frontends/ink-ui/src/slashCommands.ts`
  - Add `/model` and `/llm` suggestions.
- Add/modify tests:
  - `tests/test_llm_profiles.py`
  - `tests/test_agentmain_model_selection.py`
  - `tests/test_ink_bridge.py`
  - `frontends/ink-ui/src/inputController.test.ts`
  - `frontends/ink-ui/src/modelPanel.test.ts`
  - `frontends/ink-ui/src/slashCommands.test.ts`

## Configuration Design

Keep existing config valid:

```python
native_oai_config = {
    "name": "gpt-native",
    "model": "gpt-5.5",
    "apikey": "...",
    "apibase": "https://example/v1",
}
```

Add optional multi-profile config. This is the recommended shape because it is easy to copy from existing single configs and does not require users to invent Python variable names:

```python
llm_profile_configs = [
    {
        "key": "native_gpt55_config",
        "name": "gpt-native",
        "session": "NativeOAISession",
        "model": "gpt-5.5",
        "apikey": "...",
        "apibase": "https://example/v1",
    },
    {
        "key": "native_kimi_config",
        "name": "kimi-native",
        "session": "NativeOAISession",
        "model": "moonshotai/kimi-k2.6",
        "apikey": "...",
        "apibase": "https://example/v1",
    },
]
```

Expansion rule:

```python
{
    "key": "native_gpt55_config",
    "session": "NativeOAISession",
    "name": "gpt-native",
    ...
}
```

becomes a synthetic config entry:

```python
mykeys["native_gpt55_config"] = {
    "session": "NativeOAISession",
    "name": "gpt-native",
    ...
}
```

The synthetic key contains `config`, so existing `GenericAgent.load_llm_sessions()` filtering continues to work.

---

### Task 1: Expand `llm_profile_configs` Into Normal Config Entries

**Files:**
- Modify: `llmcore.py`
- Test: `tests/test_llm_profiles.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_llm_profiles.py`:

```python
import unittest

from llmcore import expand_llm_profile_configs


class LlmProfileConfigTests(unittest.TestCase):
    def test_keeps_legacy_configs(self):
        source = {
            "native_oai_config": {
                "session": "NativeOAISession",
                "name": "legacy",
                "model": "gpt-test",
            }
        }

        expanded = expand_llm_profile_configs(source)

        self.assertIn("native_oai_config", expanded)
        self.assertEqual(expanded["native_oai_config"]["name"], "legacy")

    def test_expands_profile_list_to_synthetic_configs(self):
        source = {
            "llm_profile_configs": [
                {
                    "key": "native_gpt55_config",
                    "session": "NativeOAISession",
                    "name": "gpt-native",
                    "model": "gpt-5.5",
                    "apikey": "test-key",
                    "apibase": "https://example.test/v1",
                },
                {
                    "key": "native_kimi_config",
                    "session": "NativeOAISession",
                    "name": "kimi-native",
                    "model": "moonshotai/kimi-k2.6",
                    "apikey": "test-key-2",
                    "apibase": "https://example2.test/v1",
                },
            ]
        }

        expanded = expand_llm_profile_configs(source)

        self.assertEqual(expanded["native_gpt55_config"]["model"], "gpt-5.5")
        self.assertEqual(expanded["native_kimi_config"]["name"], "kimi-native")
        self.assertNotIn("key", expanded["native_gpt55_config"])

    def test_invalid_profile_key_is_ignored(self):
        source = {
            "llm_profile_configs": [
                {
                    "key": "bad",
                    "session": "NativeOAISession",
                    "name": "bad",
                    "model": "gpt-test",
                }
            ]
        }

        expanded = expand_llm_profile_configs(source)

        self.assertNotIn("bad", expanded)

    def test_duplicate_profile_does_not_override_existing_config(self):
        source = {
            "native_oai_config": {"name": "legacy", "model": "gpt-old"},
            "llm_profile_configs": [
                {
                    "key": "native_oai_config",
                    "name": "new",
                    "model": "gpt-new",
                }
            ],
        }

        expanded = expand_llm_profile_configs(source)

        self.assertEqual(expanded["native_oai_config"]["name"], "legacy")
```

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```powershell
python -m unittest tests.test_llm_profiles
```

Expected: failure because `expand_llm_profile_configs` does not exist.

- [ ] **Step 3: Implement minimal expansion helper**

In `llmcore.py`, add:

```python
def expand_llm_profile_configs(mykeys):
    expanded = dict(mykeys or {})
    profiles = expanded.get("llm_profile_configs")
    if not isinstance(profiles, list):
        return expanded
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        key = str(profile.get("key") or "").strip()
        if not key or "config" not in key:
            continue
        if key in expanded:
            continue
        cfg = dict(profile)
        cfg.pop("key", None)
        expanded[key] = cfg
    return expanded
```

Then update `_load_mykeys()` or `reload_mykeys()` so the returned dictionary is passed through:

```python
mykeys = expand_llm_profile_configs(mykeys)
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m unittest tests.test_llm_profiles
python -m unittest discover -s tests
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add llmcore.py tests/test_llm_profiles.py
git commit -m "feat(llm): expand runtime model profiles"
```

---

### Task 2: Add Selector-Based Runtime Model API

**Files:**
- Modify: `agentmain.py`
- Test: `tests/test_agentmain_model_selection.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_agentmain_model_selection.py`:

```python
import types
import unittest

from agentmain import GenericAgent


class FakeBackend:
    def __init__(self, name, model):
        self.name = name
        self.model = model
        self.history = []


class FakeClient:
    def __init__(self, name, model):
        self.backend = FakeBackend(name, model)
        self.last_tools = "old-tools"


class ModelSelectionTests(unittest.TestCase):
    def make_agent(self):
        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        agent.llmclients = [
            FakeClient("gpt-native", "gpt-5.5"),
            FakeClient("kimi-native", "moonshotai/kimi-k2.6"),
            FakeClient("deepseek", "deepseek-v4"),
        ]
        agent.llmclient = agent.llmclients[0]
        agent.llmclient.backend.history = [{"role": "user", "content": "hi"}]
        agent.load_llm_sessions = types.MethodType(lambda self: None, agent)
        return agent

    def test_select_model_by_index_preserves_history(self):
        agent = self.make_agent()

        result = agent.select_llm("1")

        self.assertTrue(result["ok"])
        self.assertEqual(agent.llm_no, 1)
        self.assertEqual(agent.llmclient.backend.history, [{"role": "user", "content": "hi"}])
        self.assertEqual(agent.llmclient.last_tools, "")

    def test_select_model_by_unique_name_fragment(self):
        agent = self.make_agent()

        result = agent.select_llm("kimi")

        self.assertTrue(result["ok"])
        self.assertEqual(agent.llm_no, 1)

    def test_select_model_reports_ambiguous_fragment(self):
        agent = self.make_agent()

        result = agent.select_llm("native")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ambiguous")

    def test_select_model_reports_missing_selector(self):
        agent = self.make_agent()

        result = agent.select_llm("not-found")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_found")
```

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```powershell
python -m unittest tests.test_agentmain_model_selection
```

Expected: failure because `select_llm` does not exist.

- [ ] **Step 3: Implement selector API**

In `agentmain.py`, add methods near `next_llm()`:

```python
    def select_llm(self, selector):
        self.load_llm_sessions()
        selector = str(selector or "").strip()
        if not selector:
            return {"ok": False, "code": "empty", "message": "model selector is empty"}
        if selector.isdigit():
            return self._switch_llm_index(int(selector))
        lowered = selector.lower()
        matches = [
            i for i, client in enumerate(self.llmclients)
            if lowered in self.get_llm_name(client).lower()
            or lowered in getattr(client.backend, "model", "").lower()
            or lowered in getattr(client.backend, "name", "").lower()
        ]
        if len(matches) == 1:
            return self._switch_llm_index(matches[0])
        if not matches:
            return {"ok": False, "code": "not_found", "message": f"model not found: {selector}"}
        return {"ok": False, "code": "ambiguous", "message": f"ambiguous model selector: {selector}"}

    def _switch_llm_index(self, index):
        self.load_llm_sessions()
        if not (0 <= index < len(self.llmclients)):
            return {"ok": False, "code": "out_of_range", "message": f"model index out of range: {index}"}
        lastc = self.llmclient
        self.llm_no = index
        self.llmclient = self.llmclients[self.llm_no]
        try:
            self.llmclient.backend.history = lastc.backend.history
        except Exception:
            raise Exception('[ERROR] BAD Mixin config: Check your mykey.py')
        self.llmclient.last_tools = ''
        name = self.get_llm_name(model=True)
        if 'glm' in name or 'minimax' in name or 'kimi' in name:
            load_tool_schema('_cn')
        else:
            load_tool_schema()
        return {"ok": True, "index": self.llm_no, "name": self.get_llm_name(), "model": self.get_llm_name(model=True)}
```

Then rewrite `next_llm()` as:

```python
    def next_llm(self, n=-1):
        self.load_llm_sessions()
        index = (self.llm_no + 1) % len(self.llmclients) if n < 0 else int(n) % len(self.llmclients)
        result = self._switch_llm_index(index)
        if not result.get("ok"):
            raise Exception(result.get("message") or "model switch failed")
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m unittest tests.test_agentmain_model_selection
python -m unittest discover -s tests
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add agentmain.py tests/test_agentmain_model_selection.py
git commit -m "feat(agent): select llm by model selector"
```

---

### Task 3: Add `/model` Alias to TUI Frontends

**Files:**
- Modify: `frontends/tuiapp.py`
- Modify: `frontends/tuiapp_v2.py`

- [ ] **Step 1: Add command aliases**

In both TUI files, wire `"model": self._cmd_llm` into the command map next to `"llm": self._cmd_llm`.

In help text, add:

```text
/model - list models for current session
/model <n|name> - switch model for current session
```

- [ ] **Step 2: Update `_cmd_llm` to call `select_llm` when available**

Where `_cmd_llm` currently switches by `int(args[0])`, use:

```python
selector = " ".join(args).strip()
result = sess.agent.select_llm(selector) if hasattr(sess.agent, "select_llm") else None
```

If `result["ok"]`, display selected model. If not, display `result["message"]`.

- [ ] **Step 3: Manual smoke check**

Run:

```powershell
python frontends/tuiapp_v2.py
```

Then enter:

```text
/model ?
/model 0
/model gpt
```

Expected: current/list output works; valid selector switches; invalid selector returns a system message without crashing.

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
python -m unittest discover -s tests
```

Commit:

```powershell
git add frontends/tuiapp.py frontends/tuiapp_v2.py
git commit -m "feat(tui): add model command alias"
```

---

### Task 4: Add Ink Bridge Model Commands

**Files:**
- Modify: `frontends/ink_bridge.py`
- Modify: `tests/test_ink_bridge.py`

- [ ] **Step 1: Write failing bridge tests**

Add tests to `tests/test_ink_bridge.py`:

```python
def test_model_status_emits_available_models():
    events = []
    bridge = GenericAgentBridge(agent_factory=lambda: FakeAgent(), emit=events.append)

    bridge.model_status()

    event = events[-1]
    assert event["type"] == "model_status"
    assert event["models"][0]["current"] is True


def test_model_switch_uses_agent_selector():
    events = []
    agent = FakeAgent()
    bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

    bridge.model_switch("kimi")

    assert any(event["type"] == "system" and "Set model" in event["text"] for event in events)
    assert events[-1]["type"] == "model_status"
```

Extend `FakeAgent` with:

```python
def list_llms(self):
    return [
        (0, "NativeOAISession/gpt-native", self.llm_no == 0),
        (1, "NativeOAISession/kimi-native", self.llm_no == 1),
    ]

def select_llm(self, selector):
    if str(selector).lower() in {"1", "kimi"}:
        self.llm_no = 1
        return {"ok": True, "index": 1, "name": "NativeOAISession/kimi-native", "model": "moonshotai/kimi-k2.6"}
    return {"ok": False, "code": "not_found", "message": "model not found"}
```

- [ ] **Step 2: Run focused test and verify failure**

Run:

```powershell
python -m unittest tests.test_ink_bridge
```

Expected: failure because `model_status` / `model_switch` do not exist.

- [ ] **Step 3: Implement bridge methods**

In `GenericAgentBridge`:

```python
    def model_status(self) -> None:
        try:
            with backend_output_redirect():
                models = [
                    {"index": int(index), "name": str(name), "current": bool(current)}
                    for index, name, current in self.agent.list_llms()
                ]
            self.emit({"type": "model_status", "models": models})
        except Exception as exc:
            self.emit({"type": "error", "code": "model_status_failed", "message": str(exc)})

    def model_switch(self, selector: str) -> None:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return
        try:
            with backend_output_redirect():
                result = self.agent.select_llm(str(selector or ""))
            if result.get("ok"):
                self.emit({"type": "system", "text": f"Set model to {result.get('name')}"})
            else:
                self.emit({"type": "system", "text": str(result.get("message") or "model switch failed")})
        except Exception as exc:
            self.emit({"type": "error", "code": "model_switch_failed", "message": str(exc)})
        self.model_status()
```

In `run_jsonl_loop()` dispatch:

```python
        elif cmd_type == "model_status":
            bridge.model_status()
        elif cmd_type == "model_switch":
            bridge.model_switch(str(command.get("selector") or ""))
```

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_ink_bridge
python -m unittest discover -s tests
```

Commit:

```powershell
git add frontends/ink_bridge.py tests/test_ink_bridge.py
git commit -m "feat(ink): expose model switching bridge commands"
```

---

### Task 5: Add Ink Slash Parsing and Protocol Types

**Files:**
- Modify: `frontends/ink-ui/src/protocol.ts`
- Modify: `frontends/ink-ui/src/inputController.ts`
- Modify: `frontends/ink-ui/src/slashCommands.ts`
- Modify: `frontends/ink-ui/src/inputController.test.ts`
- Modify: `frontends/ink-ui/src/slashCommands.test.ts`

- [ ] **Step 1: Write failing TypeScript tests**

In `inputController.test.ts`, add:

```ts
test('handleInput opens model picker for /model', () => {
  const store = createPasteStoreForTest()
  const decision = handleInput('/model', '', { return: true }, 'idle', store)
  assert.deepEqual(decision, { value: '', action: { type: 'open_model' } })
})

test('handleInput switches model by selector', () => {
  const store = createPasteStoreForTest()
  const decision = handleInput('/model kimi', '', { return: true }, 'idle', store)
  assert.deepEqual(decision, { value: '', command: { type: 'model_switch', selector: 'kimi' } })
})

test('handleInput supports /llm alias', () => {
  const store = createPasteStoreForTest()
  const decision = handleInput('/llm 1', '', { return: true }, 'idle', store)
  assert.deepEqual(decision, { value: '', command: { type: 'model_switch', selector: '1' } })
})
```

In `slashCommands.test.ts`, assert `/model` and `/llm` are suggested.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
cd frontends\ink-ui
npm test -- inputController.test.ts slashCommands.test.ts
```

Expected: failure because protocol/action types and parser branches do not exist.

- [ ] **Step 3: Update protocol types**

In `protocol.ts`, add commands:

```ts
  | { type: 'model_status' }
  | { type: 'model_switch'; selector: string }
```

Add model event type:

```ts
export type ModelStatus = {
  index: number
  name: string
  current: boolean
}
```

Add event:

```ts
  | { type: 'model_status'; models: ModelStatus[] }
```

- [ ] **Step 4: Update input parser**

In `inputController.ts`, update action type:

```ts
action?: { type: 'open_resume' | 'open_rewind' | 'open_mcp' | 'open_model' | 'clear' | 'help' | 'status' }
```

Add parser logic before generic submit:

```ts
  if (/^\/(?:model|llm)$/.test(trimmed)) return { action: { type: 'open_model' } }
  const modelSwitch = /^\/(?:model|llm)\s+(.+)$/.exec(trimmed)
  if (modelSwitch) {
    const selector = modelSwitch[1].trim()
    if (selector === '?' || selector === 'help') return { command: { type: 'model_status' } }
    return { command: { type: 'model_switch', selector } }
  }
```

- [ ] **Step 5: Update slash suggestions**

In `slashCommands.ts`, add:

```ts
  { name: '/model', description: 'Show and switch AI models' },
  { name: '/llm', description: 'Alias for /model' },
```

- [ ] **Step 6: Run tests and commit**

Run:

```powershell
cd frontends\ink-ui
npm test -- inputController.test.ts slashCommands.test.ts
npm run typecheck
```

Commit:

```powershell
git add frontends/ink-ui/src/protocol.ts frontends/ink-ui/src/inputController.ts frontends/ink-ui/src/slashCommands.ts frontends/ink-ui/src/inputController.test.ts frontends/ink-ui/src/slashCommands.test.ts
git commit -m "feat(ink): parse model slash commands"
```

---

### Task 6: Add Ink Model Picker Panel

**Files:**
- Create: `frontends/ink-ui/src/modelPanel.ts`
- Create: `frontends/ink-ui/src/modelPanel.test.ts`
- Modify: `frontends/ink-ui/src/App.tsx`

- [ ] **Step 1: Write pure panel tests**

Create `frontends/ink-ui/src/modelPanel.test.ts`:

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { panelFromModelStatus, moveModelSelection } from './modelPanel.js'

test('panelFromModelStatus selects current model', () => {
  const panel = panelFromModelStatus({
    type: 'model_status',
    models: [
      { index: 0, name: 'NativeOAISession/gpt-native', current: false },
      { index: 1, name: 'NativeOAISession/kimi-native', current: true },
    ],
  })

  assert.equal(panel.selected, 1)
  assert.equal(panel.models[1].name, 'NativeOAISession/kimi-native')
})

test('moveModelSelection clamps to bounds', () => {
  assert.equal(moveModelSelection(0, -1, 2), 0)
  assert.equal(moveModelSelection(0, 1, 2), 1)
  assert.equal(moveModelSelection(1, 1, 2), 1)
})
```

- [ ] **Step 2: Run focused test and verify failure**

Run:

```powershell
cd frontends\ink-ui
npm test -- modelPanel.test.ts
```

Expected: failure because `modelPanel.ts` does not exist.

- [ ] **Step 3: Implement pure model panel helper**

Create `modelPanel.ts`:

```ts
import type { BridgeEvent, ModelStatus } from './protocol.js'

export type ModelPanelState = {
  models: ModelStatus[]
  selected: number
}

export function panelFromModelStatus(event: Extract<BridgeEvent, { type: 'model_status' }>): ModelPanelState {
  const selected = Math.max(0, event.models.findIndex(model => model.current))
  return { models: event.models, selected }
}

export function moveModelSelection(selected: number, delta: number, total: number): number {
  if (total <= 0) return 0
  return Math.max(0, Math.min(total - 1, selected + delta))
}
```

- [ ] **Step 4: Wire App state and rendering**

In `App.tsx`:

Add imports:

```ts
import { panelFromModelStatus, moveModelSelection, type ModelPanelState } from './modelPanel.js'
```

Add state:

```ts
const [modelPanel, setModelPanel] = React.useState<ModelPanelState | null>(null)
```

In bridge event handler:

```ts
if (event.type === 'model_status') {
  setModelPanel(panelFromModelStatus(event))
  return
}
```

When `/model` action is returned from input handling:

```ts
if (decision.action?.type === 'open_model') {
  bridgeRef.current?.send({ type: 'model_status' })
}
```

Add key handling while model panel is open:

```ts
if (modelPanel) {
  if (key.escape) {
    setModelPanel(null)
    return
  }
  if (key.upArrow) {
    setModelPanel({ ...modelPanel, selected: moveModelSelection(modelPanel.selected, -1, modelPanel.models.length) })
    return
  }
  if (key.downArrow) {
    setModelPanel({ ...modelPanel, selected: moveModelSelection(modelPanel.selected, 1, modelPanel.models.length) })
    return
  }
  if (key.return) {
    const selected = modelPanel.models[modelPanel.selected]
    if (selected) bridgeRef.current?.send({ type: 'model_switch', selector: String(selected.index) })
    setModelPanel(null)
    return
  }
}
```

Render panel near existing MCP/resume panels:

```tsx
function ModelPanelView({ panel }: { panel: ModelPanelState }) {
  return (
    <Box flexDirection="column" borderStyle="round" paddingX={1}>
      <Text bold>Models</Text>
      {panel.models.map((model, i) => (
        <Text key={model.index} color={i === panel.selected ? 'cyan' : undefined}>
          {i === panel.selected ? '>' : ' '} {model.current ? '✓' : ' '} {model.index}: {model.name}
        </Text>
      ))}
    </Box>
  )
}
```

Use ASCII fallback if the existing UI avoids check marks. The visual goal is a clear current-marker and selected-row marker.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
cd frontends\ink-ui
npm test -- modelPanel.test.ts
npm test
npm run typecheck
```

Commit:

```powershell
git add frontends/ink-ui/src/App.tsx frontends/ink-ui/src/modelPanel.ts frontends/ink-ui/src/modelPanel.test.ts
git commit -m "feat(ink): add model selection panel"
```

---

### Task 7: Document Usage Without Editing Secrets

**Files:**
- Modify: `README.md` or existing docs section that already describes Ink UI startup

- [ ] **Step 1: Add concise usage docs**

Add a section near Ink UI startup docs:

```markdown
### Runtime Model Switching

GA loads the legacy `native_oai_config` variables and optional `llm_profile_configs` entries from `mykey.py`.

Example:

```python
llm_profile_configs = [
    {
        "key": "native_gpt55_config",
        "session": "NativeOAISession",
        "name": "gpt-native",
        "model": "gpt-5.5",
        "apikey": "YOUR_API_KEY",
        "apibase": "https://YOUR_PROVIDER/v1",
    },
    {
        "key": "native_kimi_config",
        "session": "NativeOAISession",
        "name": "kimi-native",
        "model": "moonshotai/kimi-k2.6",
        "apikey": "YOUR_API_KEY",
        "apibase": "https://YOUR_PROVIDER/v1",
    },
]
```

In GA Ink UI:

- `/model` opens the model picker.
- `/model <index>` switches by index.
- `/model <name>` switches by unique name or model fragment.
- `/llm` remains an alias.
```

Do not include real API keys or provider-specific promotional text.

- [ ] **Step 2: Run docs-safe checks**

Run:

```powershell
rg -n "sk-|apiKey=|Bearer " README.md docs
```

Expected: no real secrets.

- [ ] **Step 3: Commit**

```powershell
git add README.md
git commit -m "docs: describe runtime model switching"
```

---

### Task 8: End-to-End Verification

**Files:**
- No new files unless fixes are required.

- [ ] **Step 1: Full Python verification**

Run:

```powershell
python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 2: Full Ink UI verification**

Run:

```powershell
cd frontends\ink-ui
npm test
npm run typecheck
```

Expected: all tests and typecheck pass.

- [ ] **Step 3: Manual Ink smoke test**

Run:

```powershell
ga
```

In the Ink UI:

```text
/model
```

Expected: model picker opens, current model is marked, arrow keys move selection, Enter switches, Escape closes.

Then:

```text
/model 1
/model kimi
/llm
```

Expected: direct switching works; `/llm` opens the same picker. No restart is required and the next message uses the selected backend.

- [ ] **Step 4: Final commit if any verification fixes were made**

```powershell
git status --short
git add <only files changed for this feature>
git commit -m "fix(ink): verify runtime model switching"
```

## Self-Review

- Spec coverage:
  - Avoid manual comment/uncomment: covered by `llm_profile_configs`.
  - No restart after startup: covered by `select_llm` and bridge commands.
  - Reference Claude Code `/model`: covered by picker, direct selector, current/status behavior.
  - Keep `native_oai_config` compatibility for existing TUI use: covered by expansion preserving legacy configs.
  - Support `tuiapp` and GA Ink: covered by Tasks 3 through 6.
  - TDD: every implementation task starts with failing tests.
- Placeholder scan:
  - No implementation step depends on undefined behavior. The only manual check is explicit smoke testing.
- Type consistency:
  - `model_status`, `model_switch`, `ModelStatus`, `ModelPanelState`, and `select_llm()` names are consistent across Python and TypeScript tasks.
