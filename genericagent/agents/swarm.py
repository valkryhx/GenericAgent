"""Session-level swarm host (CC ``m0184``): one object wiring the shared
collaboration state together.

CC evidence (v2.1.283):

- ``m0184`` ``ti()`` returns ``y.of(j().host)`` -- a **per-host lazy singleton**
  (``q`` from ``chunk-4rk7cwxz``) holding seven slots::

      inbound          = class o   # cross-session inbound gate (subagent_inbound_gate)
      receipts         = class d   # outstandingSends / awaitingTerminal
      outbound         = class l   # pacer
      ingress          = class a   # ownUdsHopToken / messageDropped
      swarmPermissions = class u   # pending / pendingSandbox / pendingPlanApproval
      mailbox          = class p   # reportedDroppedEntries / pendingPrunes / ledger
      taskList         = class m   # updated event / locks / leaderTeamName

- ``mailbox.ledger`` (class ``i``) is the idempotent-write bookkeeper:
  ``record`` keys ``msg_id -> sha256(from, timestamp, text, summary, color,
  from_plugin)``; ``wrote`` reports whether that exact entry was already written;
  ``retire``/``retain``/``forget`` manage the per-writer bucket; the map is capped
  at ``h = 2048`` (FIFO eviction), and a *retained* writer is exempt from eviction.

GA keeps the same shape but persists it on the durable file bus so the lead and
every teammate process share one view. The transport-only slots (``outbound``
pacer, ``ingress`` UDS hop token) are intentionally omitted: GA has no
cross-machine peer transport.

【复刻时暂时不需要参考 claudecode 云端会话的实现】
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from genericagent.agents.state import atomic_write_json, cross_process_lock, now_iso, read_json_or_none


# CC `m0184` h = 2048
MAILBOX_LEDGER_CAP = 2048


def mailbox_entry_digest(entry):
    """CC ``m0184 t(entry)``: sha256 over the six canonical entry fields.

    Missing fields hash as JSON ``null`` so an entry recorded with a field absent
    never matches one recorded with that field empty.
    """
    entry = entry or {}
    fields = [
        entry.get("from"),
        entry.get("timestamp"),
        entry.get("text"),
        entry.get("summary"),
        entry.get("color"),
        entry.get("from_plugin"),
    ]
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MailboxLedger:
    """Idempotent-write bookkeeper for the mailbox (CC ``m0184`` class ``i``).

    File: ``<base_dir>/mailbox_ledger.json`` -- ``{writer: {msg_id: digest}}``.
    Bounded at ``cap`` entries per writer (FIFO), mirroring CC's in-memory map.
    """

    def __init__(self, base_dir, *, cap=MAILBOX_LEDGER_CAP):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "mailbox_ledger.json"
        self.lock_path = self.path.with_name("mailbox_ledger.json.lock")
        self.cap = int(cap)
        # CC keeps this in memory (a Set of writers whose entries are pinned).
        self._retained = set()

    def record(self, writer, entry):
        """Record ``entry`` under ``writer``; returns its digest or ``None``."""
        msg_id = _msg_id(entry)
        if not msg_id:
            return None
        digest = mailbox_entry_digest(entry)
        writer_key = str(writer or "")
        with cross_process_lock(self.lock_path):
            data = self._read()
            bucket = data.setdefault(writer_key, {})
            bucket[str(msg_id)] = digest
            if writer_key not in self._retained:
                self._evict(bucket)
            atomic_write_json(self.path, data)
        return digest

    def wrote(self, writer, entry):
        """True when this exact entry was already recorded (CC ``wrote``)."""
        msg_id = _msg_id(entry)
        if not msg_id:
            return False
        bucket = (self._read().get(str(writer or "")) or {})
        return bucket.get(str(msg_id)) == mailbox_entry_digest(entry)

    def retire(self, writer, ids):
        """Drop the given ids for ``writer`` (CC ``retire``)."""
        drop = {str(item) for item in (ids or ())}
        with cross_process_lock(self.lock_path):
            data = self._read()
            bucket = data.get(str(writer or ""))
            if bucket is None:
                return
            for key in list(bucket):
                if key in drop:
                    del bucket[key]
            if not bucket:
                data.pop(str(writer or ""), None)
            atomic_write_json(self.path, data)

    def retain(self, writer, ids):
        """Pin ``writer`` and keep only ``ids`` (CC ``retain``)."""
        writer_key = str(writer or "")
        self._retained.add(writer_key)
        keep = {str(item) for item in (ids or ())}
        with cross_process_lock(self.lock_path):
            data = self._read()
            bucket = data.get(writer_key)
            if bucket is None:
                return
            data[writer_key] = {key: value for key, value in bucket.items() if key in keep}
            if not data[writer_key]:
                data.pop(writer_key, None)
            atomic_write_json(self.path, data)

    def forget(self, writer):
        """Forget every entry for ``writer`` (CC ``forget``)."""
        with cross_process_lock(self.lock_path):
            data = self._read()
            if data.pop(str(writer or ""), None) is not None:
                atomic_write_json(self.path, data)

    def entries(self, writer):
        return dict(self._read().get(str(writer or "")) or {})

    def _evict(self, bucket):
        while len(bucket) > self.cap:
            first = next(iter(bucket))
            del bucket[first]

    def _read(self):
        data = read_json_or_none(self.path)
        if not isinstance(data, dict):
            return {}
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


class TaskListState:
    """Task-board update signal + leader team name (CC ``m0184`` class ``m``)."""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "task_list_state.json"
        self.lock_path = self.path.with_name("task_list_state.json.lock")
        self._leader_team_name = None

    @property
    def leader_team_name(self):
        return self._leader_team_name

    @leader_team_name.setter
    def leader_team_name(self, value):
        self._leader_team_name = value

    def mark_updated(self, *, team_name=None):
        """Bump the update sequence and return it (CC ``updated.emit()``)."""
        if team_name is not None:
            self._leader_team_name = team_name
        with cross_process_lock(self.lock_path):
            data = self._read()
            seq = int(data.get("seq") or 0) + 1
            atomic_write_json(
                self.path,
                {"seq": seq, "at": now_iso(), "leader_team_name": self._leader_team_name},
            )
        return seq

    def updated_seq(self):
        return int(self._read().get("seq") or 0)

    def reset(self):
        if self._leader_team_name is None:
            return
        self._leader_team_name = None
        self.mark_updated()

    def _read(self):
        data = read_json_or_none(self.path)
        return data if isinstance(data, dict) else {}


class SubagentSwarm:
    """The GA host singleton: one object owning the shared collaboration state."""

    def __init__(self, base_dir, *, policy=None, hold_buffer=None, max_outstanding=None, receipts=None):
        from genericagent.agents.inbound_gate import HOLD_BUFFER, InboundGate
        from genericagent.agents.receipts import DEFAULT_MAX_OUTSTANDING, SubagentSendLedger

        self.base_dir = Path(base_dir)
        self.receipts = receipts if receipts is not None else SubagentSendLedger(
            self.base_dir, max_outstanding=max_outstanding or DEFAULT_MAX_OUTSTANDING
        )
        self.mailbox_ledger = MailboxLedger(self.base_dir)
        self.task_list = TaskListState(self.base_dir)
        self.inbound = InboundGate(
            self.base_dir,
            policy=policy,
            hold_buffer=hold_buffer or HOLD_BUFFER,
            ledger=self.mailbox_ledger,
            receipts=self.receipts,
        )
        # CC `swarmPermissions` (class u): in-process teammate permission requests.
        self.permissions = {"pending": {}, "pendingSandbox": {}, "pendingPlanApproval": None}

    def reset(self):
        """Clear the per-session state (CC ``reset`` on each slot)."""
        self.inbound.reset()
        self.permissions = {"pending": {}, "pendingSandbox": {}, "pendingPlanApproval": None}
        self.task_list.reset()


_SWARM_CACHE = {}


def get_swarm(base_dir, **kwargs):
    """Per-host lazy singleton (CC ``ti()`` / ``y.of(j().host)``)."""
    key = str(Path(base_dir).resolve())
    swarm = _SWARM_CACHE.get(key)
    if swarm is None:
        swarm = SubagentSwarm(base_dir, **kwargs)
        _SWARM_CACHE[key] = swarm
    return swarm


def reset_swarm_cache():
    """Drop the process-local singleton cache (used by tests)."""
    _SWARM_CACHE.clear()


def _msg_id(entry):
    entry = entry or {}
    return entry.get("msg_id") or entry.get("message_id")
