"""Idle subscriptions: "tell me when this agent becomes available again".

``send_message`` is fire-and-forget: it queues a message and returns. When the
message is actually *processed* depends on the target's own turn loop, so a caller
that needs to act on the outcome has only two options today -- poll ``state.json``
or guess. Polling is exactly what CC forbids ("do NOT sleep, poll, or proactively
check"), and it burns tokens doing nothing.

So a send may carry ``notify_when_idle=True``: that records a one-shot subscription
for the target's next idle transition. When the agent reports idle
(``emit_idle_notification``, or the worker's own idle hook), the frame is pushed onto
the normal notification stream with ``notify=True`` and the subscription is dropped.
The main agent then learns about it at its next fold point, using the machinery that
already exists -- no new channel, no timer, no polling.

One-shot on purpose: a standing subscription would keep firing for every later idle
of an agent that is simply doing its job, which is noise, not progress. Re-subscribe
by sending again.

Distinct from ``turn_completed``: that fires for every finished turn whether or not
anyone asked, and says nothing about *availability*. An idle frame says the agent has
no work left, which is the thing a caller waiting to hand over more work needs.
"""

from __future__ import annotations

from pathlib import Path

from genericagent.agents.state import atomic_write_json, cross_process_lock, now_iso, read_json_or_none


class IdleSubscriptions:
    """Durable set of ``agent_path -> {subscribed_at, ...}`` one-shot subscriptions.

    File: ``<base_dir>/idle_subscriptions.json``. Locked because the sender and the
    target's manager are different processes sharing one file.
    """

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "idle_subscriptions.json"
        self.lock_path = self.base_dir / "idle_subscriptions.json.lock"

    def _load(self):
        return read_json_or_none(self.path) or {"schema_version": 1, "subscriptions": {}}

    def subscribe(self, agent_path, *, requested_by=None):
        """Record (or refresh) a subscription for ``agent_path``."""
        path = str(agent_path)
        with cross_process_lock(self.lock_path):
            data = self._load()
            subs = data.setdefault("subscriptions", {})
            subs[path] = {
                "subscribed_at": now_iso(),
                "requested_by": str(requested_by or ""),
            }
            data["schema_version"] = 1
            atomic_write_json(self.path, data)
        return dict(subs[path])

    def is_subscribed(self, agent_path):
        return str(agent_path) in (self._load().get("subscriptions") or {})

    def consume(self, agent_path):
        """Take the subscription if present, removing it.

        Returns the record or ``None``. Consume-on-read is what makes the notification
        one-shot: if the frame is written but the notify fails, the subscription is
        already gone, so the cost is one missed idle ping rather than a permanent
        stream of them.
        """
        path = str(agent_path)
        with cross_process_lock(self.lock_path):
            data = self._load()
            subs = data.get("subscriptions") or {}
            if path not in subs:
                return None
            record = subs.pop(path)
            data["schema_version"] = 1
            atomic_write_json(self.path, data)
            return dict(record) if isinstance(record, dict) else {}

    def list_paths(self):
        return sorted((self._load().get("subscriptions") or {}).keys())
