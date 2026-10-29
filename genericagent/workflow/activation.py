from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowActivationDecision:
    mode: str
    action: str
    confidence: float
    matched_signals: tuple[str, ...]
    task_text: str
    reason: str
    requires_fanout: bool = False
    requires_phases: bool = False
    plan_only: bool = False


_WORKFLOW_MENTION = re.compile(r"(?:\b(?:use|using|via|through)\s+(?:a\s+|the\s+)?workflows?\b|\bwith\s+(?:a\s+|the\s+)?workflows?\b|用\s*workflow|使用\s*workflow|走\s*workflow|按\s*workflow)", re.I)
_PLAN_ONLY = re.compile(r"(?:\u53ea\u89c4\u5212|\u4ec5\u89c4\u5212|\u4e0d\u8981\u6267\u884c|\u4e0d\u5b9e\u65bd|\bplan\s+only\b|\bdo\s+not\s+execute\b)", re.I)
_EXPLICIT_FANOUT = re.compile(r"(?:multi[- ]?agent|agent\s+team|subagents?|parallel\s+agents?|多智能体|多个子代理|子代理并行|并行调研|并行调查|并行分析)", re.I)
_SEARCH = re.compile(r"(?:\bsearch\b|\bresearch\b|\blook\s+up\b|\u641c\u7d22|\u8c03\u7814|\u67e5\u627e|\u67e5\u8d44\u6599|\u6536\u96c6\u6765\u6e90)", re.I)
_ARTIFACT = re.compile(r"(?:\bwrite\b|\bcreate\b|\bgenerate\b|\bsave\b|\bproduce\b|\u751f\u6210|\u5199\u5165|\u5199\u4e00\u4e2a|\u521b\u5efa|\u4fdd\u5b58|\u8f93\u51fa)", re.I)
_VERIFY = re.compile(r"(?:\bverify\b|\bvalidate\b|\btest\b|\bcheck\b|\u9a8c\u8bc1|\u6821\u9a8c|\u68c0\u67e5|\u6d4b\u8bd5)", re.I)
_SYNTHESIZE = re.compile(r"(?:\bsynthesi[sz]e\b|\bcombine\b|\bcompare\b|\u7efc\u5408|\u6574\u7406|\u6c47\u603b|\u5bf9\u6bd4)", re.I)
_MULTI_STEP = re.compile(r"(?:\bthen\b|\bafter\b|\bfirst\b|\bfinally\b|\u7136\u540e|\u518d|\u4e4b\u540e|\u6700\u540e|\u5e76|\u540c\u65f6)", re.I)


_CONVERSATIONAL_WORDS = frozenset({
    # English acknowledgements / pleasantries
    "hi", "hello", "hey", "thanks", "thankyou", "thx", "ty", "ok", "okay", "yes", "yep",
    "no", "nope", "cool", "nice", "great", "good", "awesome", "perfect", "wonderful",
    "wondeful", "brilliant", "excellent", "amazing", "beautiful", "lovely", "wow",
    "hmm", "lgtm", "done", "gotit", "understood", "ack", "np", "welcome", "cheers",
    "sure", "yay", "please", "continue", "go", "y", "n",
    # Chinese acknowledgements / pleasantries
    "\u4f60\u597d", "\u60a8\u597d", "\u55e8", "\u54c8\u55bd", "\u8c22\u8c22", "\u591a\u8c22", "\u611f\u8c22",
    "\u597d\u7684", "\u597d", "\u53ef\u4ee5", "\u884c", "\u55ef", "\u6536\u5230", "\u660e\u767d", "\u4e86\u89e3",
    "\u4e0d\u9519", "\u5f88\u68d2", "\u68d2", "\u8d5e", "\u5b8c\u7f8e", "\u5389\u5bb3", "\u725b",
    "\u54c8\u54c8", "\u8f9b\u82e6\u4e86", "\u592a\u68d2\u4e86", "\u5e72\u5f97\u597d", "\u505a\u5f97\u4e0d\u9519",
})

_TASK_TOKENS = frozenset({
    "\u91cd\u6784", "\u4f18\u5316", "\u4fee\u590d", "\u5b9e\u73b0", "\u5f00\u53d1", "\u8bbe\u8ba1",
    "\u90e8\u7f72", "\u8fc1\u79fb", "\u6392\u67e5",
    "refactor", "optimize", "fix", "implement", "migrate", "debug",
})

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _has_task_signal(text: str) -> bool:
    return bool(
        _SEARCH.search(text)
        or _ARTIFACT.search(text)
        or _VERIFY.search(text)
        or _SYNTHESIZE.search(text)
    )


def _is_conversational_turn(text: str) -> bool:
    """True for pure acknowledgements / pleasantries that must not auto-orchestrate.

    Mirrors Claude Code's ultracode rule: a standing opt-in authors a workflow for
    every *substantive* task, but stays solo on conversational turns.
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    core = "".join(ch for ch in raw.lower() if ch.isalnum())
    if not core:
        return True
    if core in _CONVERSATIONAL_WORDS:
        return True
    if core in _TASK_TOKENS:
        return False
    if len(core) <= 16 and not _CJK.search(core) and not _has_task_signal(raw):
        return True
    return False


def _decision(mode: str, action: str, task: str, *, confidence: float = 0.0,
              signals: tuple[str, ...] = (), reason: str = "", fanout: bool = False,
              phases: bool = False, plan_only: bool = False) -> WorkflowActivationDecision:
    return WorkflowActivationDecision(mode, action, confidence, signals, task, reason, fanout, phases, plan_only)


def resolve_workflow_activation(task_text: str, *, session_enabled: bool = False) -> WorkflowActivationDecision:
    """Resolve only high-confidence per-turn workflow intent; never infer taskType."""
    text = str(task_text or "").strip()
    if not text:
        return _decision("none", "none", text, reason="empty task")
    plan_only = bool(_PLAN_ONLY.search(text))
    explicit = re.match(r"^\s*/workflow\b\s*", text, re.I)
    if explicit:
        task = text[explicit.end():].strip()
        if plan_only:
            return _decision("explicit", "none", task, confidence=1.0, reason="explicit planning-only request", plan_only=True)
        fanout = bool(_EXPLICIT_FANOUT.search(task))
        return _decision("explicit", "requested", task, confidence=1.0, signals=("slash_workflow",), reason="explicit /workflow command", fanout=fanout, phases=True)
    if plan_only:
        return _decision("none", "none", text, reason="planning-only language suppresses execution activation", plan_only=True)
    if _WORKFLOW_MENTION.search(text):
        fanout = bool(_EXPLICIT_FANOUT.search(text))
        return _decision("explicit", "requested", text, confidence=1.0, signals=("workflow_mention",), reason="explicit natural-language workflow request", fanout=fanout, phases=True)
    if _EXPLICIT_FANOUT.search(text):
        return _decision("explicit", "requested", text, confidence=1.0, signals=("explicit_fanout",), reason="explicit multi-agent request", fanout=True, phases=True)
    signals = []
    if _SEARCH.search(text):
        signals.append("search")
    if _ARTIFACT.search(text):
        signals.append("artifact")
    if _VERIFY.search(text):
        signals.append("verification")
    if _SYNTHESIZE.search(text):
        signals.append("synthesis")
    has_sequence = bool(_MULTI_STEP.search(text))
    # A standing ``/workflow on`` opt-in must win over the softer semantic
    # recommendation. Otherwise a substantive task that happens to look
    # multi-signal is demoted to ``recommended`` before session mode is even
    # consulted, and the team nudge steals it from the workflow path.
    if session_enabled:
        if _is_conversational_turn(text):
            return _decision("none", "none", text, confidence=0.0, signals=tuple(signals),
                             reason="conversational turn: standing workflow mode does not auto-orchestrate chat")
        return _decision("session", "requested", text, confidence=0.95,
                         signals=tuple(signals), reason="explicit session workflow mode", fanout=True, phases=True)
    # Semantic activation requires multiple independent action classes and an
    # explicit sequence/composition marker. A lone verb never starts a workflow.
    if len(signals) >= 2 and has_sequence:
        return _decision("semantic", "recommended", text, confidence=0.9,
                         signals=tuple(signals), reason="multi-step task spans independent action classes",
                         fanout=len(signals) >= 3, phases=True)
    return _decision("none", "none", text, confidence=0.0, signals=tuple(signals), reason="insufficient independent workflow signals")


class WorkflowActivationState:
    """Bridge-scoped explicit session opt-in; ordinary per-turn activation is stateless."""

    def __init__(self) -> None:
        self.enabled = False

    def handle(self, task_text: str) -> WorkflowActivationDecision:
        text = str(task_text or "").strip()
        lowered = text.lower()
        if lowered == "/workflow on":
            self.enabled = True
            return _decision("session", "requested", "", confidence=1.0, reason="workflow session mode enabled", phases=True)
        if lowered == "/workflow off":
            self.enabled = False
            return _decision("none", "none", "", confidence=1.0, reason="workflow session mode disabled")
        return resolve_workflow_activation(text, session_enabled=self.enabled)


def workflow_opt_in_reminder(decision: WorkflowActivationDecision) -> str:
    """Build the CC-style opt-in reminder injected ahead of an explicit request.

    Mirrors Claude Code's ``ultracode`` system-reminder: it only tells the model
    that the user opted into dynamic workflow orchestration, so the model may
    author a workflow script and call the ``workflow`` tool. It never plans the
    script itself -- the main agent is the script author (direct-JS model).
    """

    reason = str(getattr(decision, "reason", "") or "explicit workflow request")
    signals = ", ".join(str(s) for s in (getattr(decision, "matched_signals", ()) or ()))
    detail = reason if not signals else f"{reason}（signals: {signals}）"
    return (
        "[WORKFLOW OPT-IN]\n"
        f"用户已明确选择 dynamic workflow 编排（{detail}）。\n"
        "请：\n"
        "1. 先调用 load_skill('workflow-authoring') 读取脚本契约；\n"
        "2. 直接写一个以纯字面量 export const meta = { name, description, phases? } 开头的\n"
        "   JavaScript 编排脚本；\n"
        "3. 用 workflow 工具（script 字段，内联传入）启动它；\n"
        "4. 不要产出 JSON 计划，不要先把脚本写进文件；\n"
        "5. 只有当本提醒存在时才这样做（opt-in 由用户给出，不是你自己推断的）。\n"
        "[/WORKFLOW OPT-IN]"
    )
