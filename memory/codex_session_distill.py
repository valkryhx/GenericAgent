"""Distill reusable coding lessons from Codex JSONL sessions.

The tool intentionally stores compressed, redacted lessons instead of raw
conversation text. It is designed for GA memory use: progress is tracked so
old sessions are skipped, while repeated high-value patterns increase lesson
confidence instead of duplicating SOP text.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "codex_distill"
DEFAULT_SOP_PATH = Path(__file__).resolve().parent / "codex_coding_sop.md"
DEFAULT_CODEX_ROOT = Path.home() / ".codex" / "sessions"
DEFAULT_ROOT_SENTINEL = "__AUTO_CODEX_SESSIONS__"
FOCUS_ORDER = ("workflow", "debugging", "testing", "communication")
MAX_SNIPPET = 220
VALID_CATEGORIES = {"workflow", "debugging", "testing", "git", "security", "frontend", "planning", "communication", "quality"}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|authorization|cookie|secret|password)\b\s*[:=]\s*['\"]?[^'\"\s,;]{4,}"),
]
PATH_PATTERNS = [
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+(?:\\[^\s\"']*)?"),
    re.compile(r"/Users/[^/\s]+(?:/[^\s\"']*)?"),
    re.compile(r"/home/[^/\s]+(?:/[^\s\"']*)?"),
]

TEST_RE = re.compile(
    r"\b(pytest|unittest|npm\s+test|pnpm\s+test|yarn\s+test|go\s+test|cargo\s+test|mvn\s+test|gradle\s+test|ruff|mypy|tsc|eslint)\b",
    re.I,
)
FAST_SEARCH_RE = re.compile(r"(^|\s)(rg|ripgrep)\s+", re.I)
READ_RE = re.compile(r"\b(Get-Content|type|cat|sed|nl|git\s+show|git\s+diff|git\s+status)\b", re.I)


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def redact_text(text: object, max_len: int = MAX_SNIPPET) -> str:
    """Remove secrets and user-local paths, then compact long text."""
    if text is None:
        return ""
    redacted = str(text).replace("\r\n", "\n").replace("\r", "\n")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("<REDACTED_SECRET>", redacted)
    for pattern in PATH_PATTERNS:
        redacted = pattern.sub("<PATH>", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    if len(redacted) > max_len:
        redacted = redacted[: max_len - 12].rstrip() + " ...[cut]"
    return redacted


def _load_jsonl(path: Path) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"type": "parse_error", "payload": {"line": line_no}}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dedupe_existing_dirs(paths: Iterable[Path]) -> list[Path]:
    seen = set()
    result = []
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        key = str(resolved).lower() if os.name == "nt" else str(resolved)
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        result.append(resolved)
    return result


def discover_codex_session_roots(
    home: Path | str | None = None,
    appdata: Path | str | None = None,
    localappdata: Path | str | None = None,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    max_cwd_parent_depth: int = 4,
) -> list[Path]:
    """Find likely Codex session directories across machines.

    Search is conservative and only returns existing directories. The explicit
    CODEX_SESSIONS_DIR environment variable wins, then common per-user paths,
    then .codex/sessions found while walking upward from the current workspace.
    """
    env = os.environ if env is None else env
    candidates: list[Path] = []
    if env.get("CODEX_SESSIONS_DIR"):
        candidates.append(Path(env["CODEX_SESSIONS_DIR"]))
    if env.get("CODEX_HOME"):
        candidates.append(Path(env["CODEX_HOME"]) / "sessions")

    home_path = Path(home) if home is not None else Path.home()
    candidates.append(home_path / ".codex" / "sessions")

    appdata_value = appdata if appdata is not None else env.get("APPDATA")
    localappdata_value = localappdata if localappdata is not None else env.get("LOCALAPPDATA")
    for base in (appdata_value, localappdata_value):
        if base:
            base_path = Path(base)
            candidates.extend([
                base_path / "Codex" / "sessions",
                base_path / ".codex" / "sessions",
            ])

    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    for parent in [cwd_path, *list(cwd_path.parents)[:max_cwd_parent_depth]]:
        candidates.append(parent / ".codex" / "sessions")

    return _dedupe_existing_dirs(candidates)


def _parse_arguments(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {"raw": raw}


def _payload_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("message"):
        return str(payload.get("message"))
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def _command_signal(command: str) -> set[str]:
    signals = set()
    if FAST_SEARCH_RE.search(command):
        signals.add("fast_search")
    if TEST_RE.search(command):
        signals.add("verification")
    if READ_RE.search(command):
        signals.add("repo_probe")
    if "git status" in command.lower():
        signals.add("git_status")
    return signals


def _lesson(lesson_id: str, category: str, title: str, guidance: str, signals: Iterable[str]) -> dict:
    return {
        "id": lesson_id,
        "category": category,
        "title": title,
        "guidance": guidance,
        "signals": sorted(set(signals)),
    }


def _build_llm_distill_prompt(session_hash: str, focus: str = "workflow") -> str:
    source = session_hash[:16] if session_hash else ""
    return "\n".join([
        "Read this redacted Codex session packet as reusable coding experience, not as raw memory.",
        "Your job is to discover 0-3 cross-project lessons that are richer than the deterministic seed observations.",
        "For each useful lesson, call `codex_lesson_update` with a short title, concrete reusable guidance, category, evidence signals, source_hash, and confidence.",
        "Use only evidence visible in this packet. Do not store private paths, raw logs, secrets, project-specific facts, or one-off task details.",
        "If the packet only supports the seed observations and no deeper reusable pattern, record nothing.",
        f"Recommended source_hash: {source}",
        f"Current focus: {focus}",
    ])


@dataclass
class SessionPacket:
    session_hash: str
    source: str
    size: int
    mtime: str
    cwd: str = ""
    user_goals: list[str] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    failure_recovery: list[dict] = field(default_factory=list)
    seed_observations: list[dict] = field(default_factory=list)
    lessons: list[dict] = field(default_factory=list)
    quality: float = 0.0
    focus: str = "workflow"
    llm_distill_prompt: str = ""
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict:
        return {
            "session_hash": self.session_hash,
            "source": self.source,
            "size": self.size,
            "mtime": self.mtime,
            "cwd": self.cwd,
            "user_goals": self.user_goals,
            "tool_counts": self.tool_counts,
            "signals": self.signals,
            "timeline": self.timeline,
            "verification_commands": self.verification_commands,
            "failure_recovery": self.failure_recovery,
            "seed_observations": self.seed_observations,
            "lessons": self.lessons,
            "quality": self.quality,
            "focus": self.focus,
            "llm_distill_prompt": self.llm_distill_prompt,
            "created_at": self.created_at,
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Codex Session Packet {self.session_hash[:12]}",
            "",
            f"- Source: {redact_text(self.source, 300)}",
            f"- Quality: {self.quality:.2f}",
            f"- Focus: {self.focus}",
            f"- CWD: {redact_text(self.cwd, 160) or '(unknown)'}",
            f"- Signals: {', '.join(self.signals) or '(none)'}",
            "",
            "## User Goals",
        ]
        lines.extend(f"- {redact_text(goal, 180)}" for goal in self.user_goals[:5])
        if not self.user_goals:
            lines.append("- (none captured)")

        lines += ["", "## Timeline"]
        if self.timeline:
            for item in self.timeline[:40]:
                command = item.get("command")
                summary = item.get("summary")
                bits = [f"- {item.get('kind', 'event')}"]
                if item.get("tool"):
                    bits.append(f"tool={item.get('tool')}")
                if command:
                    bits.append(f"command=`{redact_text(command, 180)}`")
                if "ok" in item and item.get("ok") is not None:
                    bits.append(f"ok={item.get('ok')}")
                if item.get("signals"):
                    bits.append(f"signals={','.join(item.get('signals', []))}")
                if summary:
                    bits.append(f"summary={redact_text(summary, 180)}")
                lines.append(" | ".join(bits))
        else:
            lines.append("- (none captured)")

        lines += ["", "## Verification Commands"]
        if self.verification_commands:
            lines.extend(f"- `{redact_text(cmd, 180)}`" for cmd in self.verification_commands[:12])
        else:
            lines.append("- (none captured)")

        lines += ["", "## Failure Recovery"]
        if self.failure_recovery:
            for item in self.failure_recovery[:8]:
                failed = item.get("failed") or {}
                recovered = item.get("recovered_with") or {}
                lines.append(
                    "- failed "
                    f"`{redact_text(failed.get('command') or failed.get('tool'), 120)}` "
                    "then recovered with "
                    f"`{redact_text(recovered.get('command') or recovered.get('tool'), 120)}`"
                )
        else:
            lines.append("- (none captured)")

        lines += [
            "",
            "## Seed Observations",
            "These deterministic hints are evidence for the LLM. They are not approved lessons by themselves.",
        ]
        for item in self.seed_observations:
            lines += [
                f"### {redact_text(item.get('title'), 160)}",
                f"- ID: {item.get('id', '')}",
                f"- Category: {item.get('category', '')}",
                f"- Guidance: {redact_text(item.get('guidance'), 260)}",
                f"- Evidence signals: {', '.join(item.get('signals', []))}",
                "",
            ]
        if not self.seed_observations:
            lines.append("- (none captured)")

        lines += ["", "## LLM Distillation Task", self.llm_distill_prompt or _build_llm_distill_prompt(self.session_hash, self.focus)]
        return "\n".join(lines).rstrip() + "\n"


class DistillState:
    def __init__(self, root: Path | str = DEFAULT_STATE_DIR):
        self.root = Path(root)
        self.progress_path = self.root / "progress.json"
        self.lessons_path = self.root / "lessons.jsonl"
        self.candidates_path = self.root / "candidate_lessons.jsonl"
        self.queue_dir = self.root / "queue"
        self.learned_dir = self.root / "learned_packets"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.learned_dir.mkdir(parents=True, exist_ok=True)

    def load_progress(self) -> dict:
        if not self.progress_path.exists():
            return {"version": 1, "sessions": {}}
        try:
            with open(self.progress_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": 1, "sessions": {}}

    def save_progress(self, progress: dict) -> None:
        self.ensure()
        tmp = self.progress_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.progress_path)


def _choose_focus(record: dict | None = None) -> str:
    done = set((record or {}).get("focus_done") or [])
    for focus in FOCUS_ORDER:
        if focus not in done:
            return focus
    return FOCUS_ORDER[0]


def _slug(text: str, fallback: str = "lesson") -> str:
    text = redact_text(text, 120).lower()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        # Keep common Chinese candidate titles deterministic enough without
        # relying on transliteration packages.
        if "未提交" in text or "worktree" in text:
            words = ["protect", "user", "worktree"]
        elif "验证" in text:
            words = ["verify", "before", "done"]
        elif "搜索" in text:
            words = ["fast", "search"]
    return "_".join(words[:5]) or fallback


def _has_sensitive_text(*values: object) -> bool:
    raw = "\n".join(str(v or "") for v in values)
    if any(pattern.search(raw) for pattern in SECRET_PATTERNS + PATH_PATTERNS):
        return True
    lowered = raw.lower()
    return ".env" in lowered or "private key" in lowered or "-----begin" in lowered


def _candidate_id(category: str, title: str) -> str:
    category = _slug(category, "workflow")
    if "未提交" in title or "worktree" in title.lower():
        return f"{category}_protect_user_worktree"
    return f"{category}_{_slug(title, 'lesson')}"


def _load_jsonl_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _source_prefix(source_hash: str) -> str:
    return redact_text(source_hash, 80)[:16] if source_hash else ""


def _candidate_evidence_count(candidate: dict) -> int:
    sources = [s for s in candidate.get("sources", []) if s]
    if sources:
        return len(set(sources))
    return int(candidate.get("evidence_count", 0))


def codex_lesson_update(
    state: DistillState | None = None,
    *,
    title: str,
    guidance: str,
    category: str = "workflow",
    evidence: Iterable[str] | None = None,
    source_hash: str = "",
    confidence: float = 0.5,
) -> dict:
    """Record an LLM-proposed candidate lesson after deterministic checks."""
    state = state or DistillState()
    state.ensure()
    if _has_sensitive_text(title, guidance, category, " ".join(str(x) for x in (evidence or []))):
        return {"status": "rejected", "reason": "sensitive_content_detected"}
    title = redact_text(title, 120)
    guidance = redact_text(guidance, 320)
    category = _slug(category, "workflow")
    if category not in VALID_CATEGORIES:
        category = "workflow"
    evidence = [redact_text(x, 80) for x in (evidence or []) if redact_text(x, 80)]
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5
    if not title or len(title) < 4:
        return {"status": "rejected", "reason": "title_too_short"}
    if not guidance or len(guidance) < 12:
        return {"status": "rejected", "reason": "guidance_too_short"}
    lesson_id = _candidate_id(category, title)
    candidates = {item.get("id"): item for item in _load_jsonl_records(state.candidates_path) if item.get("id")}
    current = candidates.get(lesson_id)
    if current is None:
        current = {
            "id": lesson_id,
            "category": category,
            "title": title,
            "guidance": guidance,
            "evidence": [],
            "evidence_count": 0,
            "confidence_max": 0.0,
            "sources": [],
            "created_at": _utc_now(),
        }
        candidates[lesson_id] = current
    current["evidence"] = sorted(set(current.get("evidence", [])) | set(evidence))
    current["confidence_max"] = max(float(current.get("confidence_max", 0.0)), confidence)
    source_prefix = _source_prefix(source_hash)
    if source_prefix and source_prefix not in current.get("sources", []):
        current.setdefault("sources", []).append(source_prefix)
    if source_prefix:
        current["evidence_count"] = len(set(current.get("sources", [])))
    else:
        current["evidence_count"] = max(1, int(current.get("evidence_count", 0)))
    current["updated_at"] = _utc_now()

    with open(state.candidates_path, "w", encoding="utf-8", newline="\n") as f:
        for item in sorted(candidates.values(), key=lambda x: (-_candidate_evidence_count(x), x.get("id", ""))):
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return {"status": "candidate_recorded", "candidate": current}


def extract_session_packet(path: Path | str, focus: str = "workflow") -> SessionPacket:
    path = Path(path)
    session_hash = file_sha256(path)
    stat = path.stat()
    mtime = _dt.datetime.fromtimestamp(stat.st_mtime).replace(microsecond=0).isoformat()

    cwd = ""
    user_goals: list[str] = []
    tool_counts: dict[str, int] = {}
    signals: set[str] = set()
    tool_events: list[dict] = []
    call_to_event: dict[str, dict] = {}
    timeline: list[dict] = []
    verification_commands: list[str] = []
    failure_recovery: list[dict] = []
    last_failed_event: dict | None = None
    patch_seen = False
    verification_seen = False
    verification_success = False
    probe_before_patch = False
    failed_then_success = False
    saw_failure = False

    for row in _load_jsonl(path):
        top_type = row.get("type")
        payload = row.get("payload") or {}
        if top_type == "session_meta":
            cwd = redact_text(payload.get("cwd"), 200)
        if top_type == "event_msg":
            text = _payload_text(payload)
            if text and len(user_goals) < 6:
                user_goals.append(redact_text(text, 180))
        if top_type != "response_item" or not isinstance(payload, dict):
            continue

        ptype = payload.get("type")
        if ptype == "message":
            if payload.get("role") == "user" and len(user_goals) < 6:
                text = _payload_text(payload)
                if text:
                    user_goals.append(redact_text(text, 180))
            continue
        if ptype not in {"function_call", "custom_tool_call", "web_search_call", "function_call_output", "custom_tool_call_output"}:
            continue

        if ptype in {"function_call", "custom_tool_call", "web_search_call"}:
            name = str(payload.get("name") or ptype)
            tool_counts[name] = tool_counts.get(name, 0) + 1
            args = _parse_arguments(payload.get("arguments"))
            command = str(args.get("command") or args.get("cmd") or args.get("script") or "")
            event = {
                "name": name,
                "command": redact_text(command, 300),
                "signals": sorted(_command_signal(command)),
                "ok": None,
            }
            timeline_event = {
                "kind": "tool_call",
                "tool": name,
                "command": event["command"],
                "signals": event["signals"],
                "ok": None,
            }
            event["timeline_index"] = len(timeline)
            timeline.append(timeline_event)
            call_id = payload.get("call_id")
            if call_id:
                call_to_event[str(call_id)] = event
            tool_events.append(event)
            signals.update(event["signals"])
            if name == "apply_patch" or "patch" in name:
                patch_seen = True
                signals.add("patch")
                if any("repo_probe" in set(e.get("signals", [])) or "fast_search" in set(e.get("signals", [])) for e in tool_events[:-1]):
                    probe_before_patch = True
            if "verification" in event["signals"]:
                verification_seen = True
                if event["command"] and event["command"] not in verification_commands:
                    verification_commands.append(event["command"])
            continue

        output = str(payload.get("output") or "")
        event = call_to_event.get(str(payload.get("call_id") or ""))
        if event is None and tool_events:
            event = next((e for e in reversed(tool_events) if e.get("ok") is None), None)
        if event is not None:
            ok = "Exit code: 0" in output or "\nOK" in output or "passed" in output.lower()
            failed = "Exit code: 1" in output or "FAILED" in output or "Traceback" in output
            event["ok"] = ok if ok or failed else event.get("ok")
            idx = event.get("timeline_index")
            if isinstance(idx, int) and 0 <= idx < len(timeline):
                timeline[idx]["ok"] = event["ok"]
                timeline[idx]["summary"] = redact_text(output, 180)
            if failed:
                saw_failure = True
                signals.add("failure")
                last_failed_event = {
                    "tool": event.get("name"),
                    "command": event.get("command"),
                    "signals": event.get("signals", []),
                }
            if ok:
                if saw_failure:
                    failed_then_success = True
                    signals.add("recovery")
                    if last_failed_event:
                        failure_recovery.append({
                            "failed": last_failed_event,
                            "recovered_with": {
                                "tool": event.get("name"),
                                "command": event.get("command"),
                                "signals": event.get("signals", []),
                            },
                        })
                        last_failed_event = None
                if "verification" in set(event.get("signals", [])):
                    verification_success = True

    seed_observations = []
    if probe_before_patch or (patch_seen and any(s in signals for s in ("repo_probe", "fast_search"))):
        seed_observations.append(_lesson(
            "repo_probe_before_edit",
            "workflow",
            "Probe repository facts before editing",
            "For coding work, inspect project rules, status, and nearby code before making the smallest viable patch.",
            ["repo_probe", "patch"] + sorted(signals & {"fast_search", "git_status"}),
        ))
    if "fast_search" in signals:
        seed_observations.append(_lesson(
            "prefer_fast_text_search",
            "workflow",
            "Use fast text search to orient in code",
            "Reach for precise repository search before broad directory traversal when locating files, symbols, or tests.",
            ["fast_search"],
        ))
    if patch_seen and (verification_success or verification_seen):
        seed_observations.append(_lesson(
            "verify_changes_before_done",
            "testing",
            "Verify changed behavior before claiming completion",
            "After edits, run the focused test or project verification command and report failures instead of assuming success.",
            ["patch", "verification"] + (["verification_success"] if verification_success else []),
        ))
    if failed_then_success:
        seed_observations.append(_lesson(
            "failure_recovery_with_new_information",
            "debugging",
            "Recover from failures by adding information",
            "On failure, read the error, gather new state, then change strategy; avoid repeating the same command without new evidence.",
            ["failure", "recovery"],
        ))

    quality = 0.0
    if patch_seen:
        quality += 0.25
    if any(s in signals for s in ("repo_probe", "fast_search")):
        quality += 0.20
    if verification_seen:
        quality += 0.20
    if verification_success:
        quality += 0.15
    if failed_then_success:
        quality += 0.15
    if seed_observations:
        quality += 0.10
    quality = min(1.0, quality)

    return SessionPacket(
        session_hash=session_hash,
        source=str(path),
        size=stat.st_size,
        mtime=mtime,
        cwd=cwd,
        user_goals=[g for i, g in enumerate(user_goals) if g and g not in user_goals[:i]][:6],
        tool_counts=tool_counts,
        signals=sorted(signals),
        timeline=timeline[:80],
        verification_commands=verification_commands[:20],
        failure_recovery=failure_recovery[:12],
        seed_observations=seed_observations,
        lessons=[],
        quality=quality,
        focus=focus,
        llm_distill_prompt=_build_llm_distill_prompt(session_hash, focus),
    )


def iter_session_files(roots: Iterable[Path | str]) -> Iterable[Path]:
    for root in roots:
        root = Path(root).expanduser()
        if root.is_file() and root.suffix.lower() == ".jsonl":
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.jsonl"))


def _resolve_roots(roots: Iterable[Path | str] | None) -> list[Path | str]:
    roots = list(roots or [])
    if not roots or roots == [DEFAULT_ROOT_SENTINEL]:
        discovered = discover_codex_session_roots()
        return discovered or [DEFAULT_CODEX_ROOT]
    return roots


def prepare_sessions(
    roots: Iterable[Path | str],
    state: DistillState | None = None,
    limit: int = 3,
    min_quality: float = 0.55,
) -> list[SessionPacket]:
    state = state or DistillState()
    roots = _resolve_roots(roots)
    state.ensure()
    progress = state.load_progress()
    prepared: list[SessionPacket] = []

    for path in iter_session_files(roots):
        if len(prepared) >= limit:
            break
        try:
            session_hash = file_sha256(path)
            stat = path.stat()
        except OSError:
            continue
        key = f"sha256:{session_hash}"
        record = progress["sessions"].get(key)
        if record and record.get("status") in {"prepared", "learned", "skipped"} and record.get("size") == stat.st_size:
            continue

        focus = _choose_focus(record)
        packet = extract_session_packet(path, focus=focus)
        if packet.quality < min_quality or not (packet.seed_observations or packet.timeline):
            progress["sessions"][key] = {
                "path": str(path),
                "size": stat.st_size,
                "mtime": packet.mtime,
                "status": "skipped",
                "quality": packet.quality,
                "reason": "low_quality_or_no_lessons",
                "learn_count": (record or {}).get("learn_count", 0),
                "focus_done": (record or {}).get("focus_done", []),
                "updated_at": _utc_now(),
            }
            continue

        packet_path = state.queue_dir / f"{packet.session_hash[:16]}-{packet.focus}.json"
        with open(packet_path, "w", encoding="utf-8") as f:
            json.dump(packet.to_dict(), f, ensure_ascii=False, indent=2)
        with open(packet_path.with_suffix(".md"), "w", encoding="utf-8") as f:
            f.write(packet.to_markdown())

        progress["sessions"][key] = {
            "path": str(path),
            "size": stat.st_size,
            "mtime": packet.mtime,
            "status": "prepared",
            "quality": packet.quality,
            "learn_count": (record or {}).get("learn_count", 0),
            "focus_done": (record or {}).get("focus_done", []),
            "packet": str(packet_path),
            "updated_at": _utc_now(),
        }
        prepared.append(packet)

    state.save_progress(progress)
    return prepared


def _load_lessons(path: Path) -> dict[str, dict]:
    lessons = {}
    if not path.exists():
        return lessons
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            lesson_id = item.get("id")
            if lesson_id:
                lessons[lesson_id] = item
    return lessons


def _write_lessons(path: Path, lessons: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for item in sorted(lessons.values(), key=lambda x: (-int(x.get("evidence_count", 0)), x.get("id", ""))):
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def learn_from_packets(state: DistillState | None = None, limit: int = 5) -> int:
    state = state or DistillState()
    state.ensure()
    progress = state.load_progress()
    lessons = _load_lessons(state.lessons_path)
    learned = 0

    for packet_path in sorted(state.queue_dir.glob("*.json")):
        if learned >= limit:
            break
        try:
            with open(packet_path, "r", encoding="utf-8") as f:
                packet = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        packet_lessons = packet.get("lessons") or []
        if not packet_lessons:
            continue

        source_hash = packet.get("session_hash", "")
        source_key = f"sha256:{source_hash}" if source_hash else ""
        for lesson in packet_lessons:
            lesson_id = lesson.get("id")
            if not lesson_id:
                continue
            current = lessons.get(lesson_id)
            if current is None:
                current = {
                    "id": lesson_id,
                    "category": lesson.get("category", "workflow"),
                    "title": lesson.get("title", lesson_id),
                    "guidance": lesson.get("guidance", ""),
                    "signals": sorted(set(lesson.get("signals", []))),
                    "evidence_count": 0,
                    "sources": [],
                    "quality_max": 0.0,
                    "created_at": _utc_now(),
                }
                lessons[lesson_id] = current
            current["evidence_count"] = int(current.get("evidence_count", 0)) + 1
            current["quality_max"] = max(float(current.get("quality_max", 0.0)), float(packet.get("quality", 0.0)))
            current["signals"] = sorted(set(current.get("signals", [])) | set(lesson.get("signals", [])))
            if source_hash and source_hash[:16] not in current["sources"]:
                current["sources"].append(source_hash[:16])
            current["updated_at"] = _utc_now()

        record = progress["sessions"].get(source_key)
        if record is not None:
            record["status"] = "learned"
            record["learn_count"] = int(record.get("learn_count", 0)) + 1
            focus_done = list(record.get("focus_done") or [])
            focus = packet.get("focus", "workflow")
            if focus not in focus_done:
                focus_done.append(focus)
            record["focus_done"] = focus_done
            record["updated_at"] = _utc_now()

        learned += 1
        target = state.learned_dir / packet_path.name
        shutil.move(str(packet_path), str(target))
        md_path = packet_path.with_suffix(".md")
        if md_path.exists():
            shutil.move(str(md_path), str(target.with_suffix(".md")))

    _write_lessons(state.lessons_path, lessons)
    state.save_progress(progress)
    return learned


def promote_candidates(
    state: DistillState | None = None,
    min_evidence: int = 2,
    min_confidence: float = 0.85,
) -> int:
    """Promote validated candidate lessons into formal lessons."""
    state = state or DistillState()
    state.ensure()
    candidates = _load_jsonl_records(state.candidates_path)
    lessons = _load_lessons(state.lessons_path)
    promoted = 0
    for candidate in candidates:
        evidence_count = _candidate_evidence_count(candidate)
        if evidence_count < min_evidence and float(candidate.get("confidence_max", 0.0)) < min_confidence:
            continue
        lesson_id = candidate.get("id")
        if not lesson_id:
            continue
        current = lessons.get(lesson_id)
        if current is None:
            current = {
                "id": lesson_id,
                "category": candidate.get("category", "workflow"),
                "title": candidate.get("title", lesson_id),
                "guidance": candidate.get("guidance", ""),
                "signals": sorted(set(candidate.get("evidence", []))),
                "evidence_count": 0,
                "sources": [],
                "quality_max": float(candidate.get("confidence_max", 0.0)),
                "created_at": _utc_now(),
            }
            lessons[lesson_id] = current
            promoted += 1
        current["evidence_count"] = max(int(current.get("evidence_count", 0)), evidence_count)
        current["quality_max"] = max(float(current.get("quality_max", 0.0)), float(candidate.get("confidence_max", 0.0)))
        current["signals"] = sorted(set(current.get("signals", [])) | set(candidate.get("evidence", [])))
        current["sources"] = sorted(set(current.get("sources", [])) | set(candidate.get("sources", [])))
        current["updated_at"] = _utc_now()
    _write_lessons(state.lessons_path, lessons)
    return promoted


def render_sop(state: DistillState | None = None, output_path: Path | str = DEFAULT_SOP_PATH) -> str:
    state = state or DistillState()
    lessons = _load_lessons(state.lessons_path)
    output_path = Path(output_path)
    grouped: dict[str, list[dict]] = {}
    for item in lessons.values():
        grouped.setdefault(item.get("category", "workflow"), []).append(item)

    lines = [
        "# Codex Coding SOP",
        "",
        "来源：由 `codex_session_distill.py` 从本机 Codex JSONL 会话生成脱敏证据包，再由 LLM 读取 packet 并通过 `codex_lesson_update` 提议、校验、晋升。本文只保留跨项目编码工作法，不保存原始对话、密钥、私有路径或一次性业务事实。",
        "",
        "## 使用原则",
        "- 先按当前仓库规范执行；本 SOP 只提供编码协作习惯和避坑策略。",
        "- 经验应来自 LLM 对脱敏 packet 的分析；规则命中的 seed observation 只作为证据提示，不能单独成为正式经验。",
        "- 经验有独立证据计数，证据越多优先级越高；单次会话经验只作为弱提示。",
        "- 若本 SOP 与项目 AGENTS/CONTRIBUTING/用户指令冲突，以上游明确指令为准。",
        "",
    ]
    if not grouped:
        lines += ["## 当前状态", "- 尚未学习到足够高质量的 Codex 会话经验。", ""]
    for category in sorted(grouped):
        lines.append(f"## {category}")
        rows = sorted(grouped[category], key=lambda x: (-int(x.get("evidence_count", 0)), x.get("title", "")))
        for item in rows:
            signals = ", ".join(item.get("signals", []))
            lines += [
                f"### {redact_text(item.get('title'), 160)}",
                f"- 做法：{redact_text(item.get('guidance'), 320)}",
                f"- 证据: {int(item.get('evidence_count', 0))} | signals: {signals or '(none)'}",
                "",
            ]
    text = "\n".join(lines).rstrip() + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return text


def status_text(state: DistillState | None = None) -> str:
    state = state or DistillState()
    progress = state.load_progress()
    counts: dict[str, int] = {}
    for record in progress.get("sessions", {}).values():
        status = record.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    lessons = _load_lessons(state.lessons_path)
    candidates = _load_jsonl_records(state.candidates_path)
    queued = len(list(state.queue_dir.glob("*.json"))) if state.queue_dir.exists() else 0
    return (
        f"sessions={sum(counts.values())} "
        f"prepared={counts.get('prepared', 0)} learned={counts.get('learned', 0)} "
        f"skipped={counts.get('skipped', 0)} queued={queued} candidates={len(candidates)} lessons={len(lessons)}"
    )


def deep_distill_instructions(packets: list[SessionPacket], state: DistillState | None = None) -> str:
    state = state or DistillState()
    lines = [
        "# Codex LLM Deep Distill Batch",
        "",
        "LLM is the distillation core. The parser only prepared redacted evidence packets and deterministic seed observations.",
        "For each queued packet markdown below, read the packet and call `codex_lesson_update` for 0-3 reusable lessons supported by the evidence.",
        "Do not write raw Codex logs, secrets, private paths, or project-specific facts into memory.",
        "",
        "## Queued Packets",
    ]
    if packets:
        for packet in packets:
            md_path = state.queue_dir / f"{packet.session_hash[:16]}-{packet.focus}.md"
            lines.append(f"- {md_path}")
    else:
        lines.append("- No new packets prepared. Check `status` for queued packets from earlier runs.")
        if state.queue_dir.exists():
            for md_path in sorted(state.queue_dir.glob("*.md"))[:20]:
                lines.append(f"- {md_path}")
    lines += [
        "",
        "## After LLM Review",
        "Run `python memory/codex_session_distill.py promote` to promote validated candidates and render `memory/codex_coding_sop.md`.",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Distill reusable coding lessons from Codex JSONL sessions.")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="Progress and lesson state directory.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="Count candidate Codex JSONL files.")
    scan.add_argument("roots", nargs="*", default=[DEFAULT_ROOT_SENTINEL], help="Files or directories to scan. Defaults to auto-discovered .codex/sessions.")

    prepare = sub.add_parser("prepare", help="Parse sessions and enqueue redacted lesson packets.")
    prepare.add_argument("roots", nargs="*", default=[DEFAULT_ROOT_SENTINEL], help="Files or directories to scan. Defaults to auto-discovered .codex/sessions.")
    prepare.add_argument("--limit", type=int, default=3)
    prepare.add_argument("--min-quality", type=float, default=0.55)

    deep = sub.add_parser("deep", help="Prepare redacted packets and print LLM review instructions.")
    deep.add_argument("roots", nargs="*", default=[DEFAULT_ROOT_SENTINEL], help="Files or directories to scan. Defaults to auto-discovered .codex/sessions.")
    deep.add_argument("--limit", type=int, default=3)
    deep.add_argument("--min-quality", type=float, default=0.55)

    learn = sub.add_parser("learn", help="Merge queued packets that already contain LLM-approved lessons into lessons.jsonl.")
    learn.add_argument("--limit", type=int, default=5)

    candidate = sub.add_parser("candidate", help="Record one LLM-proposed candidate lesson.")
    candidate.add_argument("--title", required=True)
    candidate.add_argument("--guidance", required=True)
    candidate.add_argument("--category", default="workflow")
    candidate.add_argument("--evidence", action="append", default=[])
    candidate.add_argument("--source-hash", default="")
    candidate.add_argument("--confidence", type=float, default=0.5)

    promote = sub.add_parser("promote", help="Promote strong candidates into formal lessons.")
    promote.add_argument("--min-evidence", type=int, default=2)
    promote.add_argument("--min-confidence", type=float, default=0.85)
    promote.add_argument("--output", default=str(DEFAULT_SOP_PATH), help="Rendered SOP output path.")

    render = sub.add_parser("render", help="Render lessons.jsonl to codex_coding_sop.md.")
    render.add_argument("--output", default=str(DEFAULT_SOP_PATH))

    run = sub.add_parser("run", help="Legacy safe batch: prepare packets, merge any existing LLM-approved packet lessons, and render.")
    run.add_argument("roots", nargs="*", default=[DEFAULT_ROOT_SENTINEL], help="Files or directories to scan. Defaults to auto-discovered .codex/sessions.")
    run.add_argument("--limit", type=int, default=3)
    run.add_argument("--min-quality", type=float, default=0.55)
    run.add_argument("--output", default=str(DEFAULT_SOP_PATH))

    sub.add_parser("status", help="Show progress summary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state = DistillState(args.state_dir)

    if args.cmd == "scan":
        roots = _resolve_roots(args.roots)
        files = list(iter_session_files(roots))
        print(f"roots={';'.join(str(r) for r in roots)} found={len(files)}")
        return 0
    if args.cmd == "prepare":
        packets = prepare_sessions(args.roots, state=state, limit=args.limit, min_quality=args.min_quality)
        print(f"prepared={len(packets)} {status_text(state)}")
        return 0
    if args.cmd == "deep":
        packets = prepare_sessions(args.roots, state=state, limit=args.limit, min_quality=args.min_quality)
        print(deep_distill_instructions(packets, state))
        print(f"\nprepared={len(packets)} {status_text(state)}")
        return 0
    if args.cmd == "learn":
        learned = learn_from_packets(state=state, limit=args.limit)
        print(f"learned={learned} {status_text(state)}")
        return 0
    if args.cmd == "candidate":
        result = codex_lesson_update(
            state,
            title=args.title,
            guidance=args.guidance,
            category=args.category,
            evidence=args.evidence,
            source_hash=args.source_hash,
            confidence=args.confidence,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") != "rejected" else 1
    if args.cmd == "promote":
        promoted = promote_candidates(state, min_evidence=args.min_evidence, min_confidence=args.min_confidence)
        text = render_sop(state=state, output_path=args.output)
        print(f"promoted={promoted} rendered={args.output} bytes={len(text.encode('utf-8'))} {status_text(state)}")
        return 0
    if args.cmd == "render":
        text = render_sop(state=state, output_path=args.output)
        print(f"rendered={args.output} bytes={len(text.encode('utf-8'))}")
        return 0
    if args.cmd == "run":
        packets = prepare_sessions(args.roots, state=state, limit=args.limit, min_quality=args.min_quality)
        learned = learn_from_packets(state=state, limit=args.limit)
        text = render_sop(state=state, output_path=args.output)
        print(
            f"prepared={len(packets)} learned={learned} rendered={args.output} bytes={len(text.encode('utf-8'))} "
            "note=deep_lessons_require_llm_review"
        )
        return 0
    if args.cmd == "status":
        print(status_text(state))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
