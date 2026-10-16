"""Scenario-driven activation for the GA Agent Team (aligned to Claude Code 2.1.283).

Claude Code never asks the user to type team syntax.  Its only in-band trigger is a
nudge appended to the *approved plan* tool_result
(``m0503_0xccfe7a3.js @78213``):

    "If this plan can be broken down into multiple independent tasks, consider
     spawning named teammates with the Agent tool (pass a `name`) to parallelize
     the work."

GA has no plan mode, so the analogous trigger is the user's own request: when it
carries parallel / several-independent-sub-task signals we inject the same style of
nudge ahead of the model-facing message, so a *natural* request is enough to stand
up a team (no team syntax required).

The session switch mirrors CC's ``--agent-teams`` /
``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`` gate (``m0283_0xc5c1f6e.js``): ``/team on|off``
(bridge-scoped) or ``GA_AGENT_TEAMS=1`` (process-scoped).

【复刻时暂时不需要参考 claudecode 云端会话的实现】
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from workflow_activation import _is_conversational_turn


TEAM_ENV = "GA_AGENT_TEAMS"
_TRUTHY = frozenset({"1", "true", "on", "yes", "y", "enabled"})


def agent_teams_env_enabled(env=None) -> bool:
    """``GA_AGENT_TEAMS=1`` seeds the session switch at bridge construction (CC env gate)."""
    source = os.environ if env is None else env
    return str(source.get(TEAM_ENV) or "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TeamActivationDecision:
    mode: str
    action: str
    confidence: float
    matched_signals: tuple
    reason: str
    requires_parallel: bool = False


# Explicit team request: the user names the mechanism (EN + ZH).
_EXPLICIT_TEAM = re.compile(
    r"(agent\s+teams?|multi[- ]?agent|swarm|teammates?|team\s+of\s+agents"
    r"|多个智能体|多智能体|智能体团队|agent\s*团队|团队协作|组建团队|起个团队|组个队|组队"
    r"|子代理并行|多个子代理|子代理团队)",
    re.I,
)
# Concurrency markers (EN + ZH). Weak on its own ("同时" is common prose).
_PARALLEL = re.compile(
    r"(并行|同时|分别|各自|一并|\bparallel\b|\bconcurrently\b|\bin\s+parallel\b|\bat\s+the\s+same\s+time\b)",
    re.I,
)
# Several *independent* sub-tasks (EN + ZH) -- strong on its own.
_INDEPENDENT = re.compile(
    r"(互不相关|互不依赖|彼此独立|各自独立|相互独立|独立地|多个独立|若干独立|若干个独立"
    r"|\bindependent\b|\bunrelated\b|\bdisjoint\b|\bdecoupled\b)",
    re.I,
)
# Counted fan-out: "N 个/件/份/路/条/..." or "N objects/tasks/files".
_COUNTED = re.compile(
    r"([两三四五六七八九十\d]+)\s*"
    r"(个|件|份|路|条|组|种|项|块|类|方面|任务|子任务|文件|模块|来源|对象"
    r"|agents?|tasks?|files?|modules?|sources?|items?|things?|threads?|tracks?)",
    re.I,
)
# Enumerated list markers: "1) ... 2) ..." / "A、B".
_ENUM = re.compile(r"(^\s*\d+[\.\)、]|\b[1-9]\s*\)|[A-D]\s*[、,]\s*[A-D])", re.M)
# Separator count: >=2 separators implies an enumeration of >=3 items.
_ITEM_SEP = re.compile(r"([、,，;；]|\band\b|和|以及)")


def _multi_item_count(text: str) -> int:
    return len(_ITEM_SEP.findall(text))


def _decision(mode, action, *, confidence=0.0, signals=(), reason="", requires_parallel=False):
    return TeamActivationDecision(mode, action, confidence, tuple(signals), reason, requires_parallel)


def resolve_team_activation(task_text: str, *, session_enabled: bool = False) -> TeamActivationDecision:
    """Resolve per-turn agent-team intent from a *natural* request (no team syntax needed).

    Mirrors the CC plan-approval nudge: it never plans the team, it only tells the
    model that parallel/independent work was detected so it may stand one up.
    """
    text = str(task_text or "").strip()
    if not text:
        return _decision("none", "none", reason="empty request")
    if _EXPLICIT_TEAM.search(text):
        return _decision(
            "explicit", "requested", confidence=1.0, signals=("explicit_team",),
            reason="explicit agent-team request", requires_parallel=True,
        )
    signals = []
    if _PARALLEL.search(text):
        signals.append("parallel")
    if _INDEPENDENT.search(text):
        signals.append("independent")
    if _COUNTED.search(text):
        signals.append("counted_fanout")
    if _ENUM.search(text):
        signals.append("enumerated")
    if _multi_item_count(text) >= 2:
        signals.append("multi_item")
    signal_set = set(signals)
    strong = bool({"independent", "counted_fanout", "enumerated"} & signal_set)
    parallel_plus = "parallel" in signal_set and len(signals) >= 2
    if strong or parallel_plus:
        if session_enabled:
            return _decision(
                "session", "requested", confidence=0.95, signals=tuple(signals),
                reason="agent team session mode + parallel/independent signals", requires_parallel=True,
            )
        return _decision(
            "semantic", "recommended", confidence=0.8, signals=tuple(signals),
            reason="request looks parallelizable into independent sub-tasks", requires_parallel=True,
        )
    if session_enabled and not _is_conversational_turn(text):
        return _decision(
            "session", "requested", confidence=0.9, signals=tuple(signals),
            reason="explicit agent team session mode", requires_parallel=True,
        )
    return _decision("none", "none", confidence=0.0, signals=tuple(signals),
                     reason="no parallel/independent fan-out signals")


class TeamActivationState:
    """Bridge-scoped session switch; ordinary per-turn activation is stateless.

    Mirrors ``WorkflowActivationState`` and CC's session-level gate: ``/team on|off``
    toggles it, ``GA_AGENT_TEAMS=1`` seeds it enabled.
    """

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = agent_teams_env_enabled() if enabled is None else bool(enabled)

    def handle(self, task_text: str) -> TeamActivationDecision:
        text = str(task_text or "").strip()
        lowered = text.lower()
        if lowered == "/team on":
            self.enabled = True
            return _decision("session", "requested", confidence=1.0,
                             reason="agent team session mode enabled", requires_parallel=True)
        if lowered == "/team off":
            self.enabled = False
            return _decision("none", "none", confidence=1.0, reason="agent team session mode disabled")
        return resolve_team_activation(text, session_enabled=self.enabled)


def team_activation_reminder(decision: TeamActivationDecision) -> str:
    """CC-style nudge injected ahead of a parallelizable request (model-facing only).

    Mirrors CC's plan-approval nudge: it only *suggests* standing up named teammates
    when the work can be split into independent tasks; the main agent still decides.
    """
    reason = str(getattr(decision, "reason", "") or "parallelizable request")
    signals = ", ".join(str(s) for s in (getattr(decision, "matched_signals", ()) or ()))
    detail = reason if not signals else reason + "（signals: " + signals + "）"
    return (
        "[TEAM ORCHESTRATION OPT-IN]\n"
        "本请求带有「多路并行 / 多个独立子任务」信号（" + detail + "）。\n"
        "若它确实能拆成多个互不依赖、可并行的子任务，考虑用 agent team 推进"
        "（对齐 Claude Code 的 coordinator 方法论）：\n"
        "1. 先用 task_create 把工作拆成可认领的任务（每条一个清晰的交付单元）；\n"
        "2. 再用 spawn_agent(team_mode=true) 起 1~N 个 teammate：每个 message 自包含、"
        "写入路径互不重叠；\n"
        "3. teammate 必须从共享任务板认领任务（task_update owner=自己的 agent 路径 /root/<task_name>），"
        "完成后把任务置 status=completed——任务板是进度的事实来源；\n"
        "4. 只有一条独立主线、或子任务强依赖时，就本地顺序完成，不要为了“看起来并行”而组队。\n"
        "只有当本提醒存在时才这样做（opt-in 来自用户请求，不是你自己的推断）。\n"
        "[/TEAM ORCHESTRATION OPT-IN]"
    )
