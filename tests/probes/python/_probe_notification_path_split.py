"""Do the notification writer and reader agree on a directory?

The main agent reads pending subagent notifications from one place; the manager writes
them to another. If those differ, no notification ever reaches the model -- at a fold
point or at a turn start -- and every unit test still passes, because the unit tests
build the manager the legacy way.

Constructing the bridge captures process stdout into the Ink backend log, so the report
is written to a file instead of printed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

REPORT = Path("D:/tmp/ga_probe_notification_path.txt")


def main() -> int:
    lines: list[str] = []
    out = lines.append

    bridge = None
    error = None
    try:
        from frontends.ink.bridge import GenericAgentBridge

        bridge = GenericAgentBridge(emit=lambda event: None)
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not raise
        error = f"{type(exc).__name__}: {exc}"

    verdict = 1
    if bridge is not None:
        from genericagent.agents.notifications import peek_subagent_notifications_prompt

        agent = bridge.agent
        manager = getattr(bridge, "subagent_manager", None)
        agent_project_root = str(getattr(agent, "project_root", "") or "")
        session_id = str(getattr(agent, "session_id", "") or "")

        out(f"agent.session_id                  = {session_id}")
        out(f"agent.project_root                = {agent_project_root}")
        out(f"bridge.project_roots.project_root = {bridge.project_roots.project_root}")
        out(f"bridge.project_roots.project_data_dir = {bridge.project_roots.project_data_dir}")
        out("")

        if manager is None:
            out("no manager on the bridge")
        else:
            writer = Path(str(getattr(manager, "subagents_state_dir", "")))
            reader = Path(agent_project_root) / "temp" / "subagents"
            out(f"manager.subagents_state_dir (writer)      = {writer}")
            out(f"manager.task_root           (children)    = {getattr(manager, 'task_root', None)}")
            out(f"reader path (project_root/temp/subagents) = {reader}")
            out("")
            same = writer.resolve() == reader.resolve()
            out(f"WRITER == READER : {same}")
            out(f"writer exists    : {writer.exists()}")
            out(f"reader exists    : {reader.exists()}")
            note = writer / "notifications.jsonl"
            if note.exists():
                rows = [x for x in note.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
                out(f"unread notifications sitting in the writer dir: {len(rows)}")
            out("")

            text, ack = peek_subagent_notifications_prompt(agent_project_root)
            out(f"peek through the agent's project_root -> chars={len(text)} ack={ack}")
            verdict = 0 if same else 2
    else:
        out(f"bridge construction failed: {error}")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
