"""Do the bridge's manager, the handler's manager and the notification reader agree?

Three parties care about the subagent bus directory:
  * the Ink bridge's manager  -- writes events and spawns children
  * the handler's manager     -- the one a tool call actually uses
  * the notification reader   -- what the main agent drains before its next chat

If any two disagree, results are written where nobody reads them. Every unit test builds
the manager the legacy way, where all three happen to coincide, so this must be measured
on the product path.

Writes its report to a file: constructing the bridge captures process stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

REPORT = Path("D:/tmp/ga_probe_three_way.txt")


def main() -> int:
    lines: list[str] = []
    out = lines.append
    verdict = 1

    try:
        from genericagent.core.app import build_main_handler
        from frontends.ink.bridge import GenericAgentBridge

        bridge = GenericAgentBridge(emit=lambda event: None)
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not raise
        out(f"construction failed: {type(exc).__name__}: {exc}")
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return 1

    agent = bridge.agent
    bridge_manager = getattr(bridge, "subagent_manager", None)
    out(f"agent.session_id = {getattr(agent, 'session_id', None)!r}")
    out(f"bridge manager subagents_state_dir = {getattr(bridge_manager, 'subagents_state_dir', None)}")
    out(f"bridge manager task_root           = {getattr(bridge_manager, 'task_root', None)}")

    handler = getattr(agent, "handler", None)
    if handler is None:
        try:
            handler = build_main_handler(agent, getattr(agent, "history", []) or [], getattr(agent, "tool_cwd", None))
            out("handler built directly for the probe (agent.handler was None)")
        except Exception as exc:  # noqa: BLE001
            out(f"handler build failed: {type(exc).__name__}: {exc}")
    handler_manager = None
    if handler is not None:
        getter = getattr(handler, "_get_subagent_manager", None)
        if getter is not None:
            try:
                handler_manager = getter()
            except Exception as exc:  # noqa: BLE001
                out(f"handler manager failed: {type(exc).__name__}: {exc}")
    out(f"handler manager subagents_state_dir = {getattr(handler_manager, 'subagents_state_dir', None)}")
    out(f"handler manager task_root           = {getattr(handler_manager, 'task_root', None)}")

    project_root = str(getattr(agent, "project_root", "") or "")
    from genericagent.agents.notifications import resolve_notification_bus_dir

    # Ask the reader where it will actually look, rather than re-deriving the formula
    # here -- re-deriving is what let the original split hide for so long.
    reader = Path(
        resolve_notification_bus_dir(
            project_root,
            data_root=getattr(agent, "data_root", None),
            session_id=str(getattr(agent, "session_id", "") or "").strip() or None,
        )
    )
    legacy_reader = Path(project_root) / "temp" / "subagents"
    out(f"notification reader dir (resolved)          = {reader}")
    out(f"legacy reader dir (why the old bug hid)     = {legacy_reader}")
    out("")

    def same(a, b) -> str:
        if a is None or b is None:
            return "n/a"
        try:
            return "SAME" if Path(str(a)).resolve() == Path(str(b)).resolve() else "DIFFERENT"
        except OSError:
            return "DIFFERENT"

    out(f"bridge vs handler manager : {same(getattr(bridge_manager, 'subagents_state_dir', None), getattr(handler_manager, 'subagents_state_dir', None))}")
    out(f"bridge vs reader          : {same(getattr(bridge_manager, 'subagents_state_dir', None), reader)}")
    out(f"handler vs reader         : {same(getattr(handler_manager, 'subagents_state_dir', None), reader)}")

    # What the child would mirror into: the parent passes it, it is not derived.
    child_env = getattr(bridge_manager, "subagents_state_dir", None)
    out("")
    out(f"child gets GA_SUBAGENTS_STATE_DIR = {child_env}  (from _child_popen_kwargs)")
    out(f"child mirror vs bridge manager : {same(child_env, getattr(bridge_manager, 'subagents_state_dir', None))}")
    out(f"previous guess dirname(task_dir)/subagents = {Path(getattr(bridge_manager, 'task_root', '.')) / 'subagents'} (orphan bus)")

    verdict = 0
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
