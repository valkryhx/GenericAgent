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


_PLAN_ONLY = re.compile(r"(?:\u53ea\u89c4\u5212|\u4ec5\u89c4\u5212|\u4e0d\u8981\u6267\u884c|\u4e0d\u5b9e\u65bd|\bplan\s+only\b|\bdo\s+not\s+execute\b)", re.I)
_EXPLICIT_FANOUT = re.compile(r"(?:\bmulti[- ]?agent\b|\bagent\s+team\b|\bsubagents?\b|\bparallel\s+agents?\b|\u591a\u667a\u80fd\u4f53|\u591a\u4e2a\u5b50\u4ee3\u7406|\u5b50\u4ee3\u7406\u5e76\u884c)", re.I)
_SEARCH = re.compile(r"(?:\bsearch\b|\bresearch\b|\blook\s+up\b|\u641c\u7d22|\u8c03\u7814|\u67e5\u627e|\u67e5\u8d44\u6599|\u6536\u96c6\u6765\u6e90)", re.I)
_ARTIFACT = re.compile(r"(?:\bwrite\b|\bcreate\b|\bgenerate\b|\bsave\b|\bproduce\b|\u751f\u6210|\u5199\u5165|\u5199\u4e00\u4e2a|\u521b\u5efa|\u4fdd\u5b58|\u8f93\u51fa)", re.I)
_VERIFY = re.compile(r"(?:\bverify\b|\bvalidate\b|\btest\b|\bcheck\b|\u9a8c\u8bc1|\u6821\u9a8c|\u68c0\u67e5|\u6d4b\u8bd5)", re.I)
_SYNTHESIZE = re.compile(r"(?:\bsynthesi[sz]e\b|\bcombine\b|\bcompare\b|\u7efc\u5408|\u6574\u7406|\u6c47\u603b|\u5bf9\u6bd4)", re.I)
_MULTI_STEP = re.compile(r"(?:\bthen\b|\bafter\b|\bfirst\b|\bfinally\b|\u7136\u540e|\u518d|\u4e4b\u540e|\u6700\u540e|\u5e76|\u540c\u65f6)", re.I)


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
    # Semantic activation requires multiple independent action classes and an
    # explicit sequence/composition marker. A lone verb never starts a workflow.
    if len(signals) >= 2 and has_sequence:
        return _decision("semantic", "recommended", text, confidence=0.9,
                         signals=tuple(signals), reason="multi-step task spans independent action classes",
                         fanout=len(signals) >= 3, phases=True)
    if session_enabled:
        return _decision("session", "requested", text, confidence=0.95,
                         signals=tuple(signals), reason="explicit session workflow mode", fanout=True, phases=True)
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
