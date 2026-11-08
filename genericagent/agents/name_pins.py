"""Name pins: make an agent's name a stable address, not a lookup of whoever has it now.

Agent names are reusable. ``close_agent(a)`` keeps the task dir and registry row,
and a later ``spawn_agent(a)`` gets ``a_1``, so today ``send_message("a")`` after a
respawn resolves to the closed agent -- already refused by the dead-mailbox guard,
but the same confusion reappears whenever a name does get reused.

CC solves this by pinning on first send (``sendMessagePins``, ``hei()`` in
``m1922:455-517``): the first message to a name records ``{name -> id, ref}``; a
later send that resolves the name to a *different* agent is refused with
``rebound`` instead of being silently delivered to the new holder. Its own docs are
explicit that "latest wins" is the wrong behaviour, because that is exactly how
cross-talk starts.

Why the ref: CC gives each pinned agent a short stable hash (``S_t(kind,id)`` sliced
to 6 hex chars, ``m0182:500-502``) so a refusal can say *which* agent the name used
to reach, and the caller can address that one explicitly. Ours is derived the same
way -- a truncated sha256 of the agent path -- for the same reason: an opaque id
nobody can type is not a usable disambiguator.

What is deliberately NOT done: a pin survives ``close_agent``. Deleting it would
turn "the name means agent X" back into "the name means whoever holds it", which is
precisely the failure this module exists to prevent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from genericagent.agents.state import atomic_write_json, cross_process_lock, now_iso, read_json_or_none


# CC `m0182` M = 6
REF_LENGTH = 6


def agent_ref(agent_path):
    """Short stable ref for an agent path (CC ``S_t`` with ``M = 6``).

    Stable across processes and restarts because it is a pure function of the path,
    and short enough that a refusal message can show it inline without wrapping.
    """
    digest = hashlib.sha256(str(agent_path or "").encode("utf-8", "replace")).hexdigest()
    return digest[:REF_LENGTH]


class NamePins:
    """Durable ``name -> {agent_path, run_id, ref, pinned_at}`` map.

    File: ``<base_dir>/name_pins.json``. Read-modify-write goes through a cross-process
    lock because a parent and every child manager share the same file, and two
    concurrent first-sends to the same name must not both think they created the pin.
    """

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "name_pins.json"
        self.lock_path = self.base_dir / "name_pins.json.lock"

    def _load(self):
        return read_json_or_none(self.path) or {"schema_version": 1, "pins": {}}

    def get(self, name):
        pins = (self._load().get("pins") or {})
        entry = pins.get(str(name))
        return dict(entry) if isinstance(entry, dict) else None

    def pin(self, name, agent_path, run_id=None):
        """Record the binding, or return the existing one unchanged.

        First writer wins. A second call with a *different* agent must not overwrite
        the pin -- that would silently move the address -- so this only ever fills a
        gap. Callers that care about a rebound branch on the return value.
        """
        name = str(name)
        with cross_process_lock(self.lock_path):
            data = self._load()
            pins = data.setdefault("pins", {})
            existing = pins.get(name)
            if isinstance(existing, dict):
                return dict(existing)
            entry = {
                "agent_path": str(agent_path),
                "run_id": str(run_id or ""),
                "ref": agent_ref(agent_path),
                "pinned_at": now_iso(),
            }
            pins[name] = entry
            data["schema_version"] = 1
            atomic_write_json(self.path, data)
            return dict(entry)

    def resolve(self, name, agent_path, run_id=None):
        """Check ``name`` against the pin, pinning it on first use.

        Returns ``{"kind": "proceed"|"rebound", ...}``, mirroring CC's three-way
        decision (proceed-on-same / rebound-on-different / proceed-and-pin-on-first).

        A pin recorded for the same ``agent_path`` with a different ``run_id`` is
        *not* a rebound: ``resume_agent`` deliberately relaunches the same agent under
        a new run, and refusing that would break the documented way to continue a
        finished agent. The path is the identity; the run id is just which execution.
        """
        name = str(name)
        expected_path = str(agent_path)
        with cross_process_lock(self.lock_path):
            data = self._load()
            pins = data.setdefault("pins", {})
            existing = pins.get(name)
            if isinstance(existing, dict):
                pinned_path = str(existing.get("agent_path") or "")
                if pinned_path == expected_path:
                    return {"kind": "proceed", "pin": dict(existing)}
                return {
                    "kind": "rebound",
                    "name": name,
                    "previous": dict(existing),
                    "next": {"agent_path": expected_path, "run_id": str(run_id or ""),
                             "ref": agent_ref(expected_path)},
                }
            entry = {
                "agent_path": expected_path,
                "run_id": str(run_id or ""),
                "ref": agent_ref(expected_path),
                "pinned_at": now_iso(),
            }
            pins[name] = entry
            data["schema_version"] = 1
            atomic_write_json(self.path, data)
            return {"kind": "proceed", "pin": dict(entry)}


def rebound_message(result):
    """The refusal text for a rebound pin, naming both sides.

    CC's wording (``m1928``): "'<name>' now resolves to a different agent than it did
    earlier in this conversation: earlier sends went to [<ref>], which this name no
    longer reaches. Nothing was sent." Kept close to that, plus the two ways out --
    a refusal without a next step just makes the caller retry the same name.
    """
    name = result.get("name") or ""
    previous = result.get("previous") or {}
    next_agent = result.get("next") or {}
    return (
        f"`{name}` now resolves to a different agent than it did earlier in this "
        f"conversation: earlier sends went to {previous.get('agent_path')} "
        f"[{previous.get('ref')}], which this name no longer reaches. Nothing was sent. "
        f"Address the current agent explicitly as `{next_agent.get('agent_path')}` "
        f"(ref {next_agent.get('ref')}), or resume the original with "
        f"resume_agent if that is the one you meant."
    )
