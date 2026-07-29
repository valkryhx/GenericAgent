from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from subagent_state import (
    append_jsonl_event,
    atomic_write_json,
    cross_process_lock,
    now_iso,
    read_json_or_none,
)


class SubagentEventBus:
    def __init__(self, bus_dir, publisher=None):
        self.bus_dir = Path(bus_dir)
        self.events_path = self.bus_dir / "events.jsonl"
        self.notifications_path = self.bus_dir / "notifications.jsonl"
        self.cursor_path = self.bus_dir / "event_cursor.json"
        self.notification_cursor_path = self.bus_dir / "notification_cursor.json"
        self.lock_path = self.bus_dir / "event_bus.lock"
        self.publisher = publisher

    def append_event(
        self,
        event_type,
        *,
        event_id=None,
        agent_path=None,
        run_id=None,
        task_name=None,
        status=None,
        payload=None,
        notify=False,
    ):
        event_id = event_id or None
        with self._locked():
            event_id = event_id or self._new_event_id_unlocked()
            existing = self._find_event_by_id(event_id)
            if existing is not None:
                return existing
            event_seq = self.next_event_seq()
            event = {
                "schema_version": 1,
                "event_id": event_id,
                "event_seq": event_seq,
                "type": str(event_type),
                "agent_path": agent_path,
                "run_id": run_id,
                "task_name": task_name,
                "created_at": now_iso(),
                "status": dict(status or {}),
                "payload": dict(payload or {}),
            }
            event["ts"] = event["created_at"]
            append_jsonl_event(self.events_path, event)
            atomic_write_json(self.cursor_path, {"last_event_seq": event_seq, "next_event_seq": event_seq + 1})
            if notify:
                append_jsonl_event(self.notifications_path, event)
        self._publish(event)
        return event

    def _publish(self, event):
        """Best-effort realtime fan-out; the durable JSONL append stays authoritative."""
        if self.publisher is None:
            return
        try:
            self.publisher(event)
        except Exception:
            pass

    def read_events_since(self, since_event_seq=0, *, targets=None):
        since_event_seq = int(since_event_seq or 0)
        target_set = {str(target).rstrip("/").split("/")[-1] for target in (targets or []) if str(target).strip()}
        events = []
        for event in self._read_jsonl(self.events_path):
            if int(event.get("event_seq") or 0) <= since_event_seq:
                continue
            task_name = event.get("task_name") or str(event.get("agent_path") or "").rstrip("/").split("/")[-1]
            if target_set and task_name not in target_set:
                continue
            events.append(event)
        return events

    def consume_notifications(self):
        cursor = read_json_or_none(self.notification_cursor_path) or {}
        last_seq = int(cursor.get("last_event_seq") or 0)
        notifications = [event for event in self._read_jsonl(self.notifications_path) if int(event.get("event_seq") or 0) > last_seq]
        if notifications:
            atomic_write_json(self.notification_cursor_path, {"last_event_seq": int(notifications[-1].get("event_seq") or 0)})
        return notifications

    def next_event_seq(self):
        cursor = read_json_or_none(self.cursor_path) or {}
        return int(cursor.get("next_event_seq") or 1)

    def last_event_seq(self):
        cursor = read_json_or_none(self.cursor_path) or {}
        if cursor.get("last_event_seq") is not None:
            return int(cursor.get("last_event_seq") or 0)
        last = 0
        for event in self._read_jsonl(self.events_path):
            last = max(last, int(event.get("event_seq") or 0))
        return last

    def _new_event_id(self):
        with self._locked():
            return self._new_event_id_unlocked()

    def _new_event_id_unlocked(self):
        return f"evt_{self.next_event_seq():06d}"

    def _find_event_by_id(self, event_id):
        for event in self._read_jsonl(self.events_path):
            if event.get("event_id") == event_id:
                return event
        return None

    @contextmanager
    def _locked(self):
        self.bus_dir.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(self.lock_path):
            yield

    def _read_jsonl(self, path):
        path = Path(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
