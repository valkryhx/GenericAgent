from __future__ import annotations

import json
from pathlib import Path

from subagent_event_bus import SubagentEventBus


def build_subagent_notifications_prompt(root_dir):
    bus = SubagentEventBus(Path(root_dir) / "temp" / "subagents")
    notifications = bus.consume_notifications()
    if not notifications:
        return ""
    lines = ["", "[GA_SUBAGENT_NOTIFICATIONS]"]
    for event in notifications:
        payload = dict(event.get("payload") or {})
        payload.pop("final_output", None)
        notification = {
            "notification_id": event.get("event_id"),
            "event_seq": event.get("event_seq"),
            "type": event.get("type"),
            "agent_path": event.get("agent_path"),
            "run_id": event.get("run_id"),
            "status": event.get("status") or {},
            "summary": payload.get("summary") or _summary_for_event(event),
            "final_output_ref": payload.get("final_output_ref") or event.get("final_output_ref"),
        }
        lines.append("<ga_subagent_notification>")
        lines.append(json.dumps(notification, ensure_ascii=False, indent=2))
        lines.append("</ga_subagent_notification>")
    lines.extend(["[/GA_SUBAGENT_NOTIFICATIONS]", ""])
    return "\n".join(lines)


def _summary_for_event(event):
    event_type = event.get("type") or "subagent_update"
    agent_path = event.get("agent_path") or event.get("task_name") or "subagent"
    return f"{agent_path} emitted {event_type}"
