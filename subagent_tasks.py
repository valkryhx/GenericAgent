"""Shared task board for the GA Agent Team.

Aligned to Claude Code 2.1.283 ``TaskCreate`` / ``TaskGet`` / ``TaskUpdate`` /
``TaskList`` and the claim semantics in module ``m0300`` (``claimTask`` /
``claimTaskWithBusyCheck`` / ``N4e`` reclaim).

Design notes
------------
* One JSON file per list (``tasks.json``) rewritten atomically under a
  cross-process lock.  This matches the GA registry / mailbox convention
  instead of CC's per-task files + ``.highwatermark`` side file, but keeps the
  same observable semantics.
* ``owner`` is a *soft lock*: at most one owner may hold a task at a time, and a
  claim by a second owner is rejected with ``already_claimed`` (CC CAS).
* ``blockedBy`` gates claiming: a task with an incomplete blocker cannot be
  claimed (``blocked``), and ``TaskList`` only surfaces *incomplete* blockers.
* ``id`` allocation uses a monotonic high-water mark so ids are never reused
  after a delete.
"""

from __future__ import annotations

import re
from pathlib import Path

from subagent_state import atomic_write_json, cross_process_lock, now_iso, read_json_or_none


STATUSES = ("pending", "in_progress", "completed")
DELETED = "deleted"
_INTERNAL_METADATA_KEY = "_internal"
_SAFE = re.compile(r"[^a-zA-Z0-9_-]")
_UNSET = object()


def sanitize_list_id(value) -> str:
    """Map an arbitrary team/list name to a filesystem-safe segment (CC ``BT``)."""
    text = _SAFE.sub("-", str(value or "").strip())
    return text or "default"


class SubagentTaskStore:
    def __init__(self, root_dir, list_id="default"):
        self.root_dir = Path(root_dir)
        self.list_id = sanitize_list_id(list_id)
        self.dir = self.root_dir / "temp" / "teams" / self.list_id
        self.tasks_path = self.dir / "tasks.json"
        self.lock_path = self.dir / "tasks.lock"

    # ------------------------------------------------------------------ reads
    def _load_unlocked(self) -> dict:
        data = read_json_or_none(self.tasks_path)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", 1)
        data.setdefault("next_id", 1)
        if not isinstance(data.get("tasks"), list):
            data["tasks"] = []
        try:
            data["next_id"] = int(data.get("next_id") or 1)
        except (TypeError, ValueError):
            data["next_id"] = 1
        return data

    def _save_unlocked(self, data: dict) -> None:
        atomic_write_json(self.tasks_path, data)

    @staticmethod
    def _find_unlocked(data: dict, task_id: str):
        for task in data["tasks"]:
            if str(task.get("id")) == str(task_id):
                return task
        return None

    def get_task(self, task_id) -> dict | None:
        data = self._load_unlocked()
        task = self._find_unlocked(data, task_id)
        return dict(task) if task is not None else None

    def list_tasks(self, *, include_internal: bool = False) -> list[dict]:
        data = self._load_unlocked()
        open_ids = {str(t.get("id")) for t in data["tasks"] if t.get("status") != "completed"}
        summaries = []
        for task in sorted(data["tasks"], key=lambda t: _int_or(t.get("id"), 0)):
            metadata = task.get("metadata") or {}
            if not include_internal and metadata.get(_INTERNAL_METADATA_KEY):
                continue
            summaries.append(
                {
                    "id": task.get("id"),
                    "subject": task.get("subject"),
                    "status": task.get("status"),
                    "owner": task.get("owner"),
                    # Only *incomplete* blockers matter to a claimer (CC TaskList).
                    "blockedBy": [str(b) for b in (task.get("blockedBy") or []) if str(b) in open_ids],
                }
            )
        return summaries

    # ----------------------------------------------------------------- writes
    def create_task(self, subject, description="", *, active_form=None, metadata=None) -> dict:
        subject = str(subject or "").strip()
        if not subject:
            raise ValueError("subject is required")
        with cross_process_lock(self.lock_path):
            data = self._load_unlocked()
            task_id = self._new_id_unlocked(data)
            stamp = now_iso()
            task = {
                "id": task_id,
                "subject": subject,
                "description": str(description or ""),
                "activeForm": active_form,
                "owner": None,
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
                "metadata": dict(metadata or {}),
                "createdAt": stamp,
                "updatedAt": stamp,
            }
            data["tasks"].append(task)
            self._save_unlocked(data)
            return dict(task)

    def _new_id_unlocked(self, data: dict) -> str:
        taken = {str(t.get("id")) for t in data["tasks"]}
        candidate = int(data.get("next_id") or 1)
        while str(candidate) in taken:
            candidate += 1
        data["next_id"] = candidate + 1
        return str(candidate)

    def update_task(
        self,
        task_id,
        *,
        status=None,
        subject=None,
        description=None,
        active_form=None,
        owner=_UNSET,
        add_blocks=None,
        add_blocked_by=None,
        metadata=None,
    ) -> dict:
        task_id = str(task_id)
        with cross_process_lock(self.lock_path):
            data = self._load_unlocked()
            task = self._find_unlocked(data, task_id)
            if task is None:
                return {"success": False, "taskId": task_id, "error": "task_not_found"}
            if status == DELETED:
                data["tasks"].remove(task)
                self._detach_edges_unlocked(data, task_id)
                self._save_unlocked(data)
                return {"success": True, "taskId": task_id, "updatedFields": ["status"], "statusChange": "deleted"}
            updated_fields = []
            if status is not None:
                if status not in STATUSES:
                    return {"success": False, "taskId": task_id, "error": f"invalid_status:{status}"}
                task["status"] = status
                updated_fields.append("status")
            if subject is not None:
                task["subject"] = str(subject)
                updated_fields.append("subject")
            if description is not None:
                task["description"] = str(description)
                updated_fields.append("description")
            if active_form is not None:
                task["activeForm"] = active_form
                updated_fields.append("activeForm")
            if owner is not _UNSET:
                if owner is None:
                    task["owner"] = None
                else:
                    claim = self._claim_unlocked(data, task, owner, check_busy=False)
                    if not claim["success"]:
                        return claim
                updated_fields.append("owner")
            if add_blocks:
                for other in add_blocks:
                    other = str(other)
                    if other and other not in task["blocks"]:
                        task["blocks"].append(other)
                    self._add_reverse_unlocked(data, task_id, other, "blockedBy")
                updated_fields.append("blocks")
            if add_blocked_by:
                for other in add_blocked_by:
                    other = str(other)
                    if other and other not in task["blockedBy"]:
                        task["blockedBy"].append(other)
                    self._add_reverse_unlocked(data, task_id, other, "blocks")
                updated_fields.append("blockedBy")
            if metadata is not None:
                task.setdefault("metadata", {}).update(metadata)
                updated_fields.append("metadata")
            task["updatedAt"] = now_iso()
            self._save_unlocked(data)
            return {"success": True, "taskId": task_id, "updatedFields": updated_fields, "task": dict(task)}

    def claim_task(self, task_id, owner, *, check_busy=False, start=False) -> dict:
        """CAS claim (CC ``claimTask``); ``start`` also flips status to in_progress."""
        task_id = str(task_id)
        owner = str(owner)
        with cross_process_lock(self.lock_path):
            data = self._load_unlocked()
            task = self._find_unlocked(data, task_id)
            if task is None:
                return {"success": False, "reason": "task_not_found"}
            result = self._claim_unlocked(data, task, owner, check_busy=check_busy)
            if not result["success"]:
                return result
            if start:
                task["status"] = "in_progress"
            task["updatedAt"] = now_iso()
            self._save_unlocked(data)
            return {"success": True, "task": dict(task)}

    @staticmethod
    def _claim_unlocked(data, task, owner, *, check_busy):
        owner = str(owner)
        if task.get("owner") and task["owner"] != owner:
            return {"success": False, "reason": "already_claimed", "task": dict(task)}
        if task.get("status") == "completed":
            return {"success": False, "reason": "already_resolved", "task": dict(task)}
        open_ids = {str(t.get("id")) for t in data["tasks"] if t.get("status") != "completed"}
        blockers = [str(b) for b in (task.get("blockedBy") or []) if str(b) in open_ids]
        if blockers:
            return {"success": False, "reason": "blocked", "task": dict(task), "blockedByTasks": blockers}
        if check_busy:
            busy = [
                str(t.get("id"))
                for t in data["tasks"]
                if t.get("status") != "completed"
                and t.get("owner") == owner
                and str(t.get("id")) != str(task.get("id"))
            ]
            if busy:
                return {"success": False, "reason": "agent_busy", "task": dict(task), "busyWithTasks": busy}
        task["owner"] = owner
        return {"success": True, "task": dict(task)}

    def find_claimable(self, owner=None) -> dict | None:
        """First pending, unowned, unblocked task (CC ``Ve``)."""
        data = self._load_unlocked()
        open_ids = {str(t.get("id")) for t in data["tasks"] if t.get("status") != "completed"}
        for task in sorted(data["tasks"], key=lambda t: _int_or(t.get("id"), 0)):
            if task.get("status") != "pending" or task.get("owner"):
                continue
            if any(str(b) in open_ids for b in (task.get("blockedBy") or [])):
                continue
            return dict(task)
        return None

    def reclaim_owner(self, owner) -> list[dict]:
        """Unassign every unfinished task owned by ``owner`` (CC ``N4e``)."""
        owner = str(owner)
        with cross_process_lock(self.lock_path):
            data = self._load_unlocked()
            reclaimed = []
            for task in data["tasks"]:
                if task.get("status") != "completed" and task.get("owner") == owner:
                    task["owner"] = None
                    task["status"] = "pending"
                    task["updatedAt"] = now_iso()
                    reclaimed.append({"id": task.get("id"), "subject": task.get("subject")})
            if reclaimed:
                self._save_unlocked(data)
            return reclaimed

    @staticmethod
    def _add_reverse_unlocked(data, task_id, other_id, field):
        other = SubagentTaskStore._find_unlocked(data, other_id)
        if other is None:
            return
        task_id = str(task_id)
        if task_id not in (other.get(field) or []):
            other.setdefault(field, []).append(task_id)

    @staticmethod
    def _detach_edges_unlocked(data, task_id):
        task_id = str(task_id)
        for task in data["tasks"]:
            task["blocks"] = [b for b in (task.get("blocks") or []) if str(b) != task_id]
            task["blockedBy"] = [b for b in (task.get("blockedBy") or []) if str(b) != task_id]

def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def next_task_prompt(task):
    """The self-driving prompt a teammate runs after claiming a task (CC ``He``).

    CC: ``Complete all open tasks. Start with task #N: <subject>\n\n<description>``.
    """
    if not task:
        return ""
    task_id = task.get("id")
    subject = task.get("subject") or ""
    description = task.get("description") or ""
    header = "Complete all open tasks. Start with task #%s: %s" % (task_id, subject)
    return header + ("\n\n" + str(description) if description else "")
