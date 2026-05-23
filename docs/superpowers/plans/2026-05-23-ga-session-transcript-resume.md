# GA Session Transcript Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GA resume from a structured session transcript instead of treating `model_responses_*.txt` as the primary session source.

**Architecture:** Keep `model_responses` as raw debug/L4 archive logs, but add a GA-native linear JSONL transcript under `temp/sessions/`. Resume becomes transcript-first and legacy-log fallback. The first version uses per-turn backend history snapshots for correctness instead of a cc-style `parentUuid` DAG.

**Tech Stack:** Python 3.10-3.13, standard-library `json`, `uuid`, `datetime`, `pathlib`, `unittest`; existing GA modules `agentmain.py`, `frontends/continue_cmd.py`, `frontends/ink_bridge.py`.

---

## Context And Design Decisions

### Current Problem

Current GA resume has two different meanings:

- `agentmain.py` `/resume` is a prompt asking the model to inspect `temp/model_responses`.
- `frontends/continue_cmd.py` `/continue` restores by parsing `=== Prompt ===` / `=== Response ===` blocks from `model_responses_*.txt`.
- Ink `/resume` calls `continue_cmd`, then reconstructs UI from the same raw log.

This has known problems:

- `GenericAgent.log_path` is `model_responses_<random6>.txt`, but `continue_cmd` excludes and snapshots `model_responses_<pid>.txt`.
- Raw LLM logs are not a session object. They do not express session start, resumed session, UI turns, checkpoints, or exact rewind boundaries.
- UI replay currently needs to infer whether a prompt is user input or auto-continuation.

### Compatibility Guarantees

This plan must not change the raw `model_responses` format. `llmcore._write_llm_log()` keeps writing the same `=== Prompt ===` / `=== Response ===` blocks, and `memory/L4_raw_sessions/compress_session.py` keeps using those files as its source material.

The new `temp/sessions/session_*.jsonl` transcript is additive. It is the new primary resume source, not a replacement for `model_responses`. Existing L1/L2/L3/L4 memory files remain untouched by the implementation.

The only intentional behavior change around `model_responses` is that `/new`, `/continue`, and transcript resume should snapshot the actual `agent.log_path` instead of guessing `model_responses_<pid>.txt`. This makes current-run log preservation more correct, but it can create more valid `model_responses_snapshot_*.txt` files than before. L4 already scans `model_responses_*.txt`, skips recent files, and deduplicates by compressed session name, so the plan includes an explicit L4 regression test before completion.

### What To Borrow From Claude Code

Borrow the principle: a resumable session must have a structured transcript as the source of truth.

Do not copy the full cc design yet:

- No `parentUuid` graph in v1.
- No full sidecar matrix for worktree/todo/subagent state in v1.
- No migration of L4 archive away from `model_responses`.

### Transcript V1 Format

Write JSONL files to `temp/sessions/session_<uuid>.jsonl`.

Required event types:

```json
{"version":1,"type":"session_start","session_id":"session_...","created_at":"2026-05-23T13:00:00+08:00","cwd":"D:\\git_repos\\ga\\GenericAgent","frontend":null}
{"version":1,"type":"turn","session_id":"session_...","turn_id":1,"created_at":"2026-05-23T13:00:20+08:00","source":"user","user_text":"hello","assistant_text":"hi","backend_history_before":[],"backend_history_after":[{"role":"user","content":[{"type":"text","text":"hello"}]},{"role":"assistant","content":[{"type":"text","text":"hi"}]}]}
{"version":1,"type":"compact","session_id":"session_...","created_at":"2026-05-23T13:10:00+08:00","message":"Compacted 20 messages into summary context.","backend_history_after":[...]}
```

Recovery rules:

- UI messages come from `turn.user_text` and `turn.assistant_text`.
- Model context comes from the latest `turn.backend_history_after` or latest `compact.backend_history_after`.
- Rewind checkpoint before each user turn uses that turn's `backend_history_before`.
- Malformed JSONL lines are ignored but counted as warnings in parser results.
- Legacy `model_responses_*.txt` remains supported if no transcript exists or the selected path is a text log.

---

## File Structure

- Create: `session_transcript.py`
  - Owns transcript path creation, append, parse, list, restore helpers.
  - Pure functions first; no frontend imports.

- Modify: `agentmain.py`
  - Create a session transcript when `GenericAgent` starts.
  - Record completed user turns after `agent_runner_loop` finishes.
  - Preserve existing `model_responses` logging.

- Modify: `frontends/continue_cmd.py`
  - Fix current log snapshot/exclusion to use `agent.log_path`.
  - List transcript sessions first, then legacy logs.
  - Dispatch restore/extract UI by path type.
  - Import upstream legacy replay fixes for tool calls and tool results.

- Modify: `frontends/ink_bridge.py`
  - Pass actual current session/log identity into resume listing.
  - Use transcript-provided rewind checkpoints when resuming transcript sessions.
  - Keep legacy path behavior for `model_responses_*.txt`.

- Tests:
  - Create: `tests/test_session_transcript.py`
  - Create: `tests/test_continue_cmd_resume.py`
  - Modify: `tests/test_ink_bridge.py`
  - Optional modify: `tests/test_compact_context.py` if compact transcript recording is added there.

---

## Task 1: Add Pure Transcript Store

**Files:**

- Create: `session_transcript.py`
- Test: `tests/test_session_transcript.py`

- [ ] **Step 1: Write failing tests for creating, listing, loading, and restoring a session**

Add `tests/test_session_transcript.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

import session_transcript


class FakeBackend:
    def __init__(self):
        self.history = []


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = "cached tools"


class FakeAgent:
    def __init__(self):
        self.history = ["old ui history"]
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.aborted = False

    def abort(self):
        self.aborted = True


class SessionTranscriptTest(unittest.TestCase):
    def test_create_session_writes_session_start_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo")

            lines = Path(path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            event = json.loads(lines[0])
            self.assertEqual(1, event["version"])
            self.assertEqual("session_start", event["type"])
            self.assertTrue(event["session_id"].startswith("session_"))
            self.assertEqual("C:/repo", event["cwd"])

    def test_record_turn_and_load_session_round_trips_ui_and_backend_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            before = []
            after = [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ]

            session_transcript.record_turn(
                path,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="hello",
                assistant_text="hi",
                backend_history_before=before,
                backend_history_after=after,
            )

            loaded = session_transcript.load_session(path)
            self.assertEqual("session_test", loaded.session_id)
            self.assertEqual(1, loaded.rounds)
            self.assertEqual("hello", loaded.preview)
            self.assertEqual(
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                loaded.ui_messages,
            )
            self.assertEqual(after, loaded.backend_history)
            self.assertEqual(before, loaded.turns[0].backend_history_before)

    def test_list_sessions_returns_newest_first_and_skips_malformed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_a")
            second = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_b")
            Path(tmp, "broken.jsonl").write_text("{not json", encoding="utf-8")
            session_transcript.record_turn(
                second,
                session_id="session_b",
                turn_id=1,
                source="user",
                user_text="newer",
                assistant_text="answer",
                backend_history_before=[],
                backend_history_after=[{"role": "user", "content": "newer"}],
            )

            sessions = session_transcript.list_sessions(root=tmp)

            self.assertEqual(["session_b", "session_a"], [s.session_id for s in sessions])
            self.assertEqual(str(second), sessions[0].path)
            self.assertEqual(str(first), sessions[1].path)

    def test_restore_session_replaces_backend_history_and_clears_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            history = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
            session_transcript.record_turn(
                path,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="hello",
                assistant_text="hi",
                backend_history_before=[],
                backend_history_after=history,
            )
            agent = FakeAgent()

            result = session_transcript.restore_agent_session(agent, path)

            self.assertTrue(result.ok)
            self.assertTrue(agent.aborted)
            self.assertEqual(history, agent.llmclient.backend.history)
            self.assertEqual("", agent.llmclient.last_tools)
            self.assertIsNone(agent.handler)
            self.assertEqual("session_test", agent.session_id)
            self.assertEqual(str(path), agent.session_path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify red**

Run:

```powershell
python -m unittest tests.test_session_transcript
```

Expected:

```text
ModuleNotFoundError: No module named 'session_transcript'
```

- [ ] **Step 3: Implement minimal transcript module**

Create `session_transcript.py` with these public functions and dataclasses:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import copy
import json
import os
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ROOT = PROJECT_ROOT / "temp" / "sessions"


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _root(root=None):
    return Path(root) if root is not None else DEFAULT_ROOT


def new_session_id():
    return "session_" + uuid.uuid4().hex


def session_path(session_id, root=None):
    return _root(root) / f"{session_id}.jsonl"


def is_transcript_path(path):
    return str(path or "").lower().endswith(".jsonl")


@dataclass
class TranscriptTurn:
    turn_id: int
    user_text: str
    assistant_text: str
    backend_history_before: list
    backend_history_after: list


@dataclass
class LoadedSession:
    path: str
    session_id: str
    mtime: float
    preview: str
    rounds: int
    ui_messages: list
    backend_history: list
    turns: list[TranscriptTurn] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RestoreResult:
    ok: bool
    message: str
    session: LoadedSession | None = None


def append_event(path, event):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def create_session(root=None, cwd=None, session_id=None, frontend=None):
    sid = session_id or new_session_id()
    path = session_path(sid, root=root)
    append_event(path, {
        "version": 1,
        "type": "session_start",
        "session_id": sid,
        "created_at": _now_iso(),
        "cwd": cwd or os.getcwd(),
        "frontend": frontend,
    })
    return str(path)


def record_turn(path, *, session_id, turn_id, source, user_text, assistant_text,
                backend_history_before, backend_history_after):
    append_event(path, {
        "version": 1,
        "type": "turn",
        "session_id": session_id,
        "turn_id": int(turn_id),
        "created_at": _now_iso(),
        "source": source,
        "user_text": user_text or "",
        "assistant_text": assistant_text or "",
        "backend_history_before": copy.deepcopy(backend_history_before or []),
        "backend_history_after": copy.deepcopy(backend_history_after or []),
    })


def record_compact(path, *, session_id, message, backend_history_after):
    append_event(path, {
        "version": 1,
        "type": "compact",
        "session_id": session_id,
        "created_at": _now_iso(),
        "message": message or "",
        "backend_history_after": copy.deepcopy(backend_history_after or []),
    })


def load_session(path):
    p = Path(path)
    warnings = []
    session_id = ""
    turns = []
    ui_messages = []
    backend_history = []
    if not p.exists():
        raise FileNotFoundError(str(path))
    for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception as exc:
            warnings.append(f"line {lineno}: {exc}")
            continue
        if not isinstance(event, dict) or event.get("version") != 1:
            warnings.append(f"line {lineno}: unsupported event")
            continue
        session_id = event.get("session_id") or session_id
        if event.get("type") == "turn":
            turn = TranscriptTurn(
                turn_id=int(event.get("turn_id") or len(turns) + 1),
                user_text=str(event.get("user_text") or ""),
                assistant_text=str(event.get("assistant_text") or ""),
                backend_history_before=copy.deepcopy(event.get("backend_history_before") or []),
                backend_history_after=copy.deepcopy(event.get("backend_history_after") or []),
            )
            turns.append(turn)
            if turn.user_text.strip():
                ui_messages.append({"role": "user", "content": turn.user_text})
            if turn.assistant_text.strip():
                ui_messages.append({"role": "assistant", "content": turn.assistant_text})
            backend_history = copy.deepcopy(turn.backend_history_after)
        elif event.get("type") == "compact":
            backend_history = copy.deepcopy(event.get("backend_history_after") or [])
    preview = next((t.user_text.strip() for t in turns if t.user_text.strip()), "")
    stat = p.stat()
    return LoadedSession(
        path=str(p),
        session_id=session_id or p.stem,
        mtime=stat.st_mtime,
        preview=preview,
        rounds=len(turns),
        ui_messages=ui_messages,
        backend_history=backend_history,
        turns=turns,
        warnings=warnings,
    )


def list_sessions(root=None, exclude_session_id=None):
    out = []
    base = _root(root)
    if not base.exists():
        return []
    for p in base.glob("session_*.jsonl"):
        try:
            loaded = load_session(p)
        except Exception:
            continue
        if exclude_session_id and loaded.session_id == exclude_session_id:
            continue
        out.append(loaded)
    out.sort(key=lambda item: item.mtime, reverse=True)
    return out


def restore_agent_session(agent, path):
    loaded = load_session(path)
    try:
        agent.abort()
    except Exception:
        pass
    backend = getattr(getattr(agent, "llmclient", None), "backend", None)
    if backend is not None and hasattr(backend, "history"):
        backend.history = copy.deepcopy(loaded.backend_history)
    if hasattr(agent, "history"):
        agent.history = []
    client = getattr(agent, "llmclient", None)
    if client is not None and hasattr(client, "last_tools"):
        client.last_tools = ""
    if hasattr(agent, "handler"):
        agent.handler = None
    agent.session_id = loaded.session_id
    agent.session_path = loaded.path
    return RestoreResult(
        ok=True,
        message=f"已恢复 {loaded.rounds} 轮结构化会话（{Path(path).name}）",
        session=loaded,
    )
```

- [ ] **Step 4: Run tests and verify green**

Run:

```powershell
python -m unittest tests.test_session_transcript
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```powershell
git add session_transcript.py tests/test_session_transcript.py
git commit -m "feat(resume): 添加结构化会话 transcript 存储"
```

---

## Task 2: Fix Current Log Boundary And Legacy Replay

**Files:**

- Modify: `frontends/continue_cmd.py`
- Test: `tests/test_continue_cmd_resume.py`

- [ ] **Step 1: Write failing tests for actual log path exclusion and snapshot**

Add `tests/test_continue_cmd_resume.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frontends import continue_cmd


class FakeBackend:
    def __init__(self):
        self.history = []


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = "cached"


class FakeAgent:
    def __init__(self, log_path):
        self.log_path = str(log_path)
        self.history = ["old"]
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.aborted = False

    def abort(self):
        self.aborted = True


def write_native_log(path, user_text="hello", assistant_text="hi"):
    prompt = {
        "role": "user",
        "content": [{"type": "text", "text": user_text}],
    }
    response = [{"type": "text", "text": assistant_text}]
    Path(path).write_text(
        "=== Prompt === 2026-05-23 13:00:00\n"
        + json.dumps(prompt, ensure_ascii=False, indent=2)
        + "\n\n=== Response === 2026-05-23 13:00:01\n"
        + repr(response)
        + "\n\n",
        encoding="utf-8",
    )


class ContinueCmdResumeTest(unittest.TestCase):
    def test_list_sessions_excludes_actual_agent_log_path_not_only_pid_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            current = log_dir / "model_responses_123456.txt"
            old = log_dir / "model_responses_999999.txt"
            write_native_log(current, "current")
            write_native_log(old, "old")
            with patch.object(continue_cmd, "_LOG_GLOB", str(log_dir / "model_responses_*.txt")):
                sessions = continue_cmd.list_sessions(exclude_path=str(current))
            self.assertEqual([str(old)], [item[0] for item in sessions])

    def test_reset_conversation_snapshots_actual_agent_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            current = log_dir / "model_responses_123456.txt"
            write_native_log(current)
            agent = FakeAgent(current)
            with patch.object(continue_cmd, "_LOG_DIR", str(log_dir)):
                message = continue_cmd.reset_conversation(agent)
            snapshots = list(log_dir.glob("model_responses_snapshot_*"))
            self.assertEqual("🆕 已开启新对话，当前上下文已清空", message)
            self.assertEqual(1, len(snapshots))
            self.assertEqual("", current.read_text(encoding="utf-8"))
            self.assertEqual([], agent.llmclient.backend.history)
            self.assertEqual("", agent.llmclient.last_tools)

    def test_legacy_extract_ui_messages_keeps_tool_result_with_assistant_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_responses_111111.txt"
            first_prompt = {"role": "user", "content": [{"type": "text", "text": "search"}]}
            first_response = [
                {"type": "text", "text": "I will search"},
                {"type": "tool_use", "id": "tool-1", "name": "web_search", "input": {"q": "x"}},
            ]
            second_prompt = {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "result text"},
                    {"type": "text", "text": "### [WORKING MEMORY]\n<history></history>"},
                ],
            }
            second_response = [{"type": "text", "text": "done"}]
            path.write_text(
                "=== Prompt === 2026-05-23 13:00:00\n"
                + json.dumps(first_prompt, ensure_ascii=False, indent=2)
                + "\n\n=== Response === 2026-05-23 13:00:01\n"
                + repr(first_response)
                + "\n\n=== Prompt === 2026-05-23 13:00:02\n"
                + json.dumps(second_prompt, ensure_ascii=False, indent=2)
                + "\n\n=== Response === 2026-05-23 13:00:03\n"
                + repr(second_response)
                + "\n\n",
                encoding="utf-8",
            )

            messages = continue_cmd.extract_ui_messages(str(path))

            self.assertEqual("user", messages[0]["role"])
            self.assertEqual("search", messages[0]["content"])
            self.assertEqual("assistant", messages[1]["role"])
            self.assertIn("Tool: `web_search`", messages[1]["content"])
            self.assertIn("result text", messages[1]["content"])
            self.assertIn("LLM Running (Turn 2)", messages[1]["content"])
            self.assertEqual(2, len(messages))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify red**

Run:

```powershell
python -m unittest tests.test_continue_cmd_resume
```

Expected:

```text
TypeError: list_sessions() got an unexpected keyword argument 'exclude_path'
```

or a failed assertion showing the current random log path was not excluded.

- [ ] **Step 3: Implement actual-path exclusion and snapshot**

Modify `frontends/continue_cmd.py`:

- Change `list_sessions(exclude_pid=None)` to `list_sessions(exclude_pid=None, exclude_path=None, exclude_session_id=None)`.
- Exclude `os.path.abspath(exclude_path)` in addition to the old PID filename.
- Change `_snapshot_current_log(pid=None)` to `_snapshot_current_log(pid=None, path=None)`.
- In `reset_conversation(agent, ...)`, call `_snapshot_current_log(path=getattr(agent, "log_path", None))`.

Implementation shape:

```python
def list_sessions(exclude_pid=None, exclude_path=None, exclude_session_id=None):
    files = glob.glob(_LOG_GLOB)
    if exclude_pid is not None:
        tag = f'model_responses_{exclude_pid}.txt'
        files = [f for f in files if not f.endswith(tag)]
    if exclude_path:
        excluded = os.path.abspath(exclude_path)
        files = [f for f in files if os.path.abspath(f) != excluded]
    ...
```

```python
def _snapshot_current_log(pid=None, path=None):
    path = path or _current_log_path(pid)
    ...
```

- [ ] **Step 4: Port upstream legacy UI replay fixes**

In `frontends/continue_cmd.py`, replace the existing `_user_text`, `_assistant_text`, `_TURN_MARK`, and `extract_ui_messages` block with the upstream logic from `upstream_check/main`:

- `_INJECT_MARKERS`
- `_format_tool_use`
- `_format_tool_result`
- `_tool_results_from_prompt`
- `_format_response_segment`
- `extract_ui_messages`

Keep `_assistant_text` public because `frontends/export_cmd.py` imports it.

- [ ] **Step 5: Run tests and verify green**

Run:

```powershell
python -m unittest tests.test_continue_cmd_resume
```

Expected:

```text
OK
```

- [ ] **Step 6: Run existing related tests**

Run:

```powershell
python -m unittest tests.test_ink_bridge tests.test_compact_context
```

Expected:

```text
OK
```

- [ ] **Step 7: Commit**

```powershell
git add frontends/continue_cmd.py tests/test_continue_cmd_resume.py
git commit -m "fix(resume): 使用真实日志路径隔离当前会话"
```

---

## Task 3: Record Transcript Turns From GenericAgent

**Files:**

- Modify: `agentmain.py`
- Modify: `session_transcript.py`
- Test: `tests/test_session_transcript.py`

- [ ] **Step 1: Add failing test for agent turn recording helper**

Append to `SessionTranscriptTest`:

```python
    def test_record_agent_turn_uses_agent_session_and_increments_turn_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            agent = FakeAgent()
            agent.session_id = "session_test"
            agent.session_path = str(path)
            agent.session_turn_id = 0
            before = []
            after = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
            agent.llmclient.backend.history = after

            session_transcript.record_agent_turn(
                agent,
                user_text="hello",
                assistant_text="hi",
                source="user",
                backend_history_before=before,
            )

            loaded = session_transcript.load_session(path)
            self.assertEqual(1, agent.session_turn_id)
            self.assertEqual(1, loaded.rounds)
            self.assertEqual(after, loaded.backend_history)
            self.assertEqual("hello", loaded.ui_messages[0]["content"])
            self.assertEqual("hi", loaded.ui_messages[1]["content"])
```

- [ ] **Step 2: Run test and verify red**

Run:

```powershell
python -m unittest tests.test_session_transcript.SessionTranscriptTest.test_record_agent_turn_uses_agent_session_and_increments_turn_id
```

Expected:

```text
AttributeError: module 'session_transcript' has no attribute 'record_agent_turn'
```

- [ ] **Step 3: Implement `ensure_agent_session` and `record_agent_turn`**

Add to `session_transcript.py`:

```python
def ensure_agent_session(agent, *, root=None, frontend=None):
    if getattr(agent, "session_path", None) and getattr(agent, "session_id", None):
        return agent.session_path
    sid = new_session_id()
    path = create_session(root=root, cwd=os.getcwd(), session_id=sid, frontend=frontend)
    agent.session_id = sid
    agent.session_path = path
    agent.session_turn_id = 0
    return path


def current_backend_history(agent):
    backend = getattr(getattr(agent, "llmclient", None), "backend", None)
    return copy.deepcopy(getattr(backend, "history", []) or [])


def record_agent_turn(agent, *, user_text, assistant_text, source, backend_history_before):
    path = ensure_agent_session(agent)
    turn_id = int(getattr(agent, "session_turn_id", 0) or 0) + 1
    agent.session_turn_id = turn_id
    record_turn(
        path,
        session_id=getattr(agent, "session_id"),
        turn_id=turn_id,
        source=source,
        user_text=user_text,
        assistant_text=assistant_text,
        backend_history_before=backend_history_before,
        backend_history_after=current_backend_history(agent),
    )
```

- [ ] **Step 4: Wire `GenericAgent.__init__`**

Modify `agentmain.py`:

```python
import session_transcript
```

In `GenericAgent.__init__`, after `self.log_path = ...`:

```python
        self.session_id = None
        self.session_path = None
        self.session_turn_id = 0
        try:
            session_transcript.ensure_agent_session(self)
        except Exception as e:
            print(f"[WARN] Failed to initialize session transcript: {e}")
```

- [ ] **Step 5: Wire `GenericAgent.run` after each completed task**

In `GenericAgent.run`, before creating `agent_runner_loop`, capture:

```python
            transcript_history_before = session_transcript.current_backend_history(self)
```

After `display_queue.put({'done': full_resp, ...})` and `self.history = handler.history_info`, record:

```python
                try:
                    session_transcript.record_agent_turn(
                        self,
                        user_text=raw_query,
                        assistant_text=full_resp,
                        source=source,
                        backend_history_before=transcript_history_before,
                    )
                except Exception as e:
                    print(f"[WARN] Failed to record session transcript: {e}")
```

In the exception path, after sending the error to the display queue, also record a failed turn with `assistant_text` containing the displayed error text. Use the same helper and keep the warning guarded.

- [ ] **Step 6: Run targeted tests**

Run:

```powershell
python -m unittest tests.test_session_transcript
```

Expected:

```text
OK
```

- [ ] **Step 7: Run agentmain smoke-related tests**

Run:

```powershell
python -m unittest tests.test_agentmain_model_selection tests.test_agentmain_llm_sessions tests.test_native_image_input
```

Expected:

```text
OK
```

- [ ] **Step 8: Commit**

```powershell
git add agentmain.py session_transcript.py tests/test_session_transcript.py
git commit -m "feat(resume): 记录每轮结构化会话快照"
```

---

## Task 4: Make `/continue` Transcript-First With Legacy Fallback

**Files:**

- Modify: `frontends/continue_cmd.py`
- Modify: `session_transcript.py`
- Test: `tests/test_continue_cmd_resume.py`

- [ ] **Step 1: Add failing tests for transcript-first listing and restore dispatch**

Append to `tests/test_continue_cmd_resume.py`:

```python
    def test_list_sessions_includes_transcripts_before_legacy_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_root = root / "sessions"
            log_dir = root / "model_responses"
            log_dir.mkdir()
            transcript = session_transcript.create_session(
                root=transcript_root,
                cwd="C:/repo",
                session_id="session_test",
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="from transcript",
                assistant_text="answer",
                backend_history_before=[],
                backend_history_after=[{"role": "user", "content": "from transcript"}],
            )
            legacy = log_dir / "model_responses_111111.txt"
            write_native_log(legacy, "from legacy")

            with (
                patch.object(continue_cmd, "_LOG_GLOB", str(log_dir / "model_responses_*.txt")),
                patch.object(continue_cmd, "_SESSION_ROOT", str(transcript_root)),
            ):
                sessions = continue_cmd.list_sessions()

            self.assertEqual(str(transcript), sessions[0][0])
            self.assertEqual("from transcript", sessions[0][2])
            self.assertEqual(str(legacy), sessions[1][0])

    def test_restore_dispatches_transcript_path_to_session_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            history = [{"role": "user", "content": "hello"}]
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="hello",
                assistant_text="hi",
                backend_history_before=[],
                backend_history_after=history,
            )
            agent = FakeAgent(Path(tmp) / "model_responses_123456.txt")

            message, ok = continue_cmd.restore(agent, str(transcript))

            self.assertTrue(ok)
            self.assertIn("结构化会话", message)
            self.assertEqual(history, agent.llmclient.backend.history)
```

Add import near the top:

```python
import session_transcript
```

- [ ] **Step 2: Run tests and verify red**

Run:

```powershell
python -m unittest tests.test_continue_cmd_resume
```

Expected:

```text
AttributeError: module 'frontends.continue_cmd' has no attribute '_SESSION_ROOT'
```

or restore not dispatching `.jsonl`.

- [ ] **Step 3: Implement transcript-first listing**

Modify `frontends/continue_cmd.py`:

```python
try:
    import session_transcript
except Exception:
    session_transcript = None

_SESSION_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'temp', 'sessions')
```

At the beginning of `list_sessions`:

```python
    out = []
    if session_transcript is not None:
        for s in session_transcript.list_sessions(root=_SESSION_ROOT, exclude_session_id=exclude_session_id):
            out.append((s.path, s.mtime, s.preview, s.rounds))
```

Then append legacy log entries as today. Sort combined output by mtime descending.

- [ ] **Step 4: Dispatch restore and extract by path type**

In `restore(agent, path)`:

```python
    if session_transcript is not None and session_transcript.is_transcript_path(path):
        try:
            result = session_transcript.restore_agent_session(agent, path)
        except Exception as e:
            return f'❌ 读取结构化会话失败: {e}', False
        return f'✅ {result.message}\n(已写入 backend.history，可直接继续)', True
```

In `extract_ui_messages(path)`:

```python
    if session_transcript is not None and session_transcript.is_transcript_path(path):
        try:
            return session_transcript.load_session(path).ui_messages
        except Exception:
            return []
```

- [ ] **Step 5: Make frontend command pass actual session exclusion**

Change `handle_frontend_command(agent, query, exclude_pid=None)` to:

```python
def handle_frontend_command(agent, query, exclude_pid=None):
    ...
    exclude_session_id = getattr(agent, "session_id", None)
    exclude_path = getattr(agent, "log_path", None)
```

Then call:

```python
sessions = list_sessions(
    exclude_pid=exclude_pid,
    exclude_path=exclude_path,
    exclude_session_id=exclude_session_id,
)
```

Keep `handle()` compatibility for old chat frontends by passing `exclude_pid=os.getpid()` and no `agent`-only assumptions.

- [ ] **Step 6: Run targeted tests**

Run:

```powershell
python -m unittest tests.test_continue_cmd_resume tests.test_session_transcript
```

Expected:

```text
OK
```

- [ ] **Step 7: Commit**

```powershell
git add frontends/continue_cmd.py session_transcript.py tests/test_continue_cmd_resume.py
git commit -m "feat(resume): 优先恢复结构化会话 transcript"
```

---

## Task 5: Make Ink Resume Use Exact Transcript Checkpoints

**Files:**

- Modify: `frontends/ink_bridge.py`
- Modify: `tests/test_ink_bridge.py`
- Optional modify: `session_transcript.py`

- [ ] **Step 1: Add failing test for transcript resume checkpoints**

Append to `InkBridgeTest` in `tests/test_ink_bridge.py`:

```python
    def test_resume_transcript_uses_exact_backend_history_before_each_turn_for_rewind(self):
        import session_transcript
        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            first_after = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "a1"}]
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="one",
                assistant_text="a1",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="two",
                assistant_text="a2",
                backend_history_before=first_after,
                backend_history_after=first_after + [{"role": "user", "content": "two"}],
            )
            agent = FakeAgent()
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            with (
                patch("ink_bridge.continue_reset", lambda *_args, **_kwargs: None),
                patch("ink_bridge.continue_restore", lambda _agent, _path: ("✅ restored", True)),
                patch("ink_bridge.continue_extract", session_transcript.load_session(str(transcript)).ui_messages),
            ):
                bridge.resume_session(str(transcript))

            self.assertEqual([], bridge._rewind_snapshots[1]["backend_history"])
            self.assertEqual(first_after, bridge._rewind_snapshots[2]["backend_history"])
```

If the patching style above conflicts with existing imports, patch only `continue_reset` and `continue_restore`, then let bridge call the real transcript loader through the new helper from Step 3.

- [ ] **Step 2: Run test and verify red**

Run:

```powershell
python -m unittest tests.test_ink_bridge.InkBridgeTest.test_resume_transcript_uses_exact_backend_history_before_each_turn_for_rewind
```

Expected:

```text
AssertionError
```

because the current implementation uses `(user_count - 1) * 2` slicing.

- [ ] **Step 3: Add transcript-aware checkpoint builder**

Modify `frontends/ink_bridge.py`:

```python
try:
    import session_transcript
except Exception:
    session_transcript = None
```

In `_resume_ui_messages_with_checkpoints`, before legacy logic:

```python
        if session_transcript is not None and session_transcript.is_transcript_path(path):
            try:
                loaded = session_transcript.load_session(path)
            except Exception:
                loaded = None
            if loaded is not None:
                messages = []
                self._task_seq = 0
                self._rewind_snapshots.clear()
                for turn in loaded.turns:
                    self._task_seq += 1
                    task_id = self._task_seq
                    messages.append({"role": "user", "text": turn.user_text, "taskId": task_id})
                    self._rewind_snapshots[task_id] = {
                        "text": turn.user_text,
                        "history": [],
                        "backend_history": copy.deepcopy(turn.backend_history_before),
                        "last_tools": "",
                    }
                    if turn.assistant_text:
                        messages.append({"role": "assistant", "text": turn.assistant_text, "taskId": task_id})
                return messages
```

- [ ] **Step 4: Pass actual exclusion identity to resume listing**

Modify `list_resume_sessions` and `resume_session_by_index` calls:

```python
sessions = continue_list(
    exclude_pid=os.getpid(),
    exclude_path=getattr(self.agent, "log_path", None),
    exclude_session_id=getattr(self.agent, "session_id", None),
)
```

- [ ] **Step 5: Run targeted tests**

Run:

```powershell
python -m unittest tests.test_ink_bridge
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit**

```powershell
git add frontends/ink_bridge.py tests/test_ink_bridge.py
git commit -m "fix(ink): 使用 transcript 恢复精确 rewind 检查点"
```

---

## Task 6: Record Compact Events Into Transcript

**Files:**

- Modify: `frontends/ink_bridge.py`
- Modify: `tests/test_ink_bridge.py`
- Optional modify: `session_transcript.py`

- [ ] **Step 1: Add failing test that compact writes transcript compact event**

Append to `tests/test_ink_bridge.py`:

```python
    def test_compact_records_transcript_compact_event(self):
        import session_transcript
        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            agent = FakeAgent()
            agent.session_id = "session_test"
            agent.session_path = str(transcript)
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
            compacted = [{"role": "user", "content": "summary"}]

            with patch("ink_bridge.compact_agent_context") as compact:
                compact.return_value.ok = True
                compact.return_value.summary = "summary text"
                compact.return_value.original_messages = 4
                compact.return_value.compacted_messages = 1
                compact.return_value.message = "Compacted 4 messages into summary context."
                agent.llmclient.backend.history = compacted
                bridge.compact("keep important details")

            loaded = session_transcript.load_session(transcript)
            self.assertEqual(compacted, loaded.backend_history)
```

- [ ] **Step 2: Run test and verify red**

Run:

```powershell
python -m unittest tests.test_ink_bridge.InkBridgeTest.test_compact_records_transcript_compact_event
```

Expected:

```text
AssertionError: [] != [{'role': 'user', 'content': 'summary'}]
```

- [ ] **Step 3: Record compact event after successful compaction**

Modify `frontends/ink_bridge.py` in `compact()` after compact succeeds and backend history has been replaced:

```python
                if session_transcript is not None and getattr(self.agent, "session_path", None):
                    try:
                        session_transcript.record_compact(
                            self.agent.session_path,
                            session_id=getattr(self.agent, "session_id", ""),
                            message=result.message,
                            backend_history_after=self._backend_history(),
                        )
                    except Exception as exc:
                        self.emit({"type": "error", "code": "compact_transcript_failed", "message": str(exc)})
```

Do not fail the compact command if transcript recording fails; compact already changed live state.

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m unittest tests.test_ink_bridge tests.test_session_transcript
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```powershell
git add frontends/ink_bridge.py tests/test_ink_bridge.py
git commit -m "feat(resume): 记录压缩后的会话上下文"
```

---

## Task 7: Full Regression And Manual Smoke Test

**Files:**

- No production changes expected.
- Update tests only if a legitimate regression is found.

- [ ] **Step 1: Run all Python tests**

Run:

```powershell
python -m unittest discover -s tests
```

Expected:

```text
OK
```

- [ ] **Step 2: Run Ink UI tests if Node dependencies are installed**

Run:

```powershell
cd frontends\ink-ui
npm test -- --runInBand
```

Expected:

```text
Tests pass
```

If dependencies are not installed, record:

```text
Skipped Ink UI npm tests because frontends/ink-ui/node_modules is missing.
```

- [ ] **Step 3: Manual smoke test in GA Ink**

Run:

```powershell
ga ink
```

Manual steps:

1. Submit: `你好，记住本轮 resume 测试`
2. Wait for completion.
3. Exit.
4. Start `ga ink` again.
5. Run `/resume`.
6. Confirm the previous conversation appears as a structured session.
7. Restore it.
8. Submit: `刚才我让你记住什么？`
9. Confirm the model has the restored backend context.

Expected filesystem check:

```powershell
Get-ChildItem temp\sessions\session_*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 3
```

The newest file should contain `session_start` and at least one `turn` line.

- [ ] **Step 4: Manual legacy fallback smoke test**

Temporarily rename `temp/sessions` out of the way or use an empty session directory, then run `/continue`.

Expected:

- Existing `model_responses_*.txt` sessions still list.
- Restoring a legacy log still writes `backend.history`.
- UI replay for tool calls includes tool call headers and result fences.

- [ ] **Step 5: Run L4 raw session compatibility smoke test**

Run a dry-run batch process against a temporary directory containing both a numeric `model_responses_*.txt` file and a `model_responses_snapshot_*.txt` file. This verifies the transcript work did not change the raw log format expected by L4.

Run:

```powershell
@'
import json
import tempfile
from pathlib import Path
from memory.L4_raw_sessions.compress_session import batch_process

def write_log(path, user):
    prompt = {"role": "user", "content": [{"type": "text", "text": user}]}
    response = [{"type": "text", "text": "<summary>ok</summary>"}]
    Path(path).write_text(
        "=== Prompt === 2026-05-20 10:00:00\n"
        + json.dumps(prompt, ensure_ascii=False, indent=2)
        + "\n\n=== Response === 2026-05-20 10:00:01\n"
        + repr(response)
        + "\n\n",
        encoding="utf-8",
    )

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    raw = root / "raw"
    l4 = root / "l4"
    raw.mkdir()
    l4.mkdir()
    write_log(raw / "model_responses_111111.txt", "numeric")
    write_log(raw / "model_responses_snapshot_111111_20260520_100000_000000001.txt", "snapshot")
    result = batch_process(str(raw), str(l4), dry_run=True)
    print(result)
    assert result["errors"] == 0
    assert result["processed"] >= 1
'@ | python -
```

Expected:

```text
errors: 0
```

The exact `processed` count may be `1` or `2` depending on timestamp-derived deduplication, but it must not error.

- [ ] **Step 6: Commit final test/doc adjustment if needed**

If only tests or comments changed during regression:

```powershell
git add <changed-files>
git commit -m "test(resume): 覆盖结构化会话恢复回归"
```

---

## Rollback Plan

If transcript restore causes regressions:

1. Keep `session_transcript.py` in place.
2. In `frontends/continue_cmd.py`, temporarily disable transcript listing by returning only legacy logs from `list_sessions`.
3. Legacy `/continue` remains functional because no existing `model_responses` path is removed.
4. Re-enable transcript listing after fixing parser/restore behavior.

---

## Acceptance Criteria

- `/resume` or `/continue` no longer depends on guessing the current log by PID.
- A new GA run creates `temp/sessions/session_*.jsonl`.
- Completed user turns append structured `turn` records with UI text and backend history snapshots.
- Resume from transcript restores `backend.history` without parsing `model_responses`.
- Ink UI history replacement comes from transcript records.
- Ink rewind after transcript resume uses exact `backend_history_before`, not `2 * user_count` slicing.
- Legacy `model_responses_*.txt` restore still works.
- L4 dry-run processing of `model_responses_*.txt` and `model_responses_snapshot_*.txt` still has zero errors.
- `python -m unittest discover -s tests` passes.

---

## Self-Review

- Spec coverage: covers current log boundary bug, transcript storage, agent recording, transcript-first restore, Ink checkpoints, compact recording, legacy fallback, and verification.
- Placeholder scan: no `TBD` or unspecified implementation tasks remain.
- Type consistency: `LoadedSession`, `TranscriptTurn`, and `RestoreResult` names are used consistently across tasks.
- Scope check: v1 intentionally avoids cc `parentUuid` DAG and sidecar metadata. This is enough to fix GA resume semantics without over-expanding the architecture.
