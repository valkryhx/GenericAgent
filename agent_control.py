from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agent_runtime_models import AgentEventBatch, AgentRecord, AgentResultRecord
from sensitive_redaction import redact_sensitive_text


@dataclass(frozen=True)
class ControlRequest:
    action: str
    reason: str = ""
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    code: str
    execution_id: str
    scope: str = "execution"
    status: str | None = None
    message: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "executionId": self.execution_id,
            "scope": self.scope,
            "status": self.status,
            "message": self.message,
            "data": dict(self.data),
        }


class AgentControlAdapter(Protocol):
    engine: str

    def list_records(self, *, include_terminal: bool = False, path_prefix: str | None = None) -> list[AgentRecord]:
        raise NotImplementedError

    def get_record(self, execution_id: str) -> AgentRecord | None:
        raise NotImplementedError

    def events_since(
        self,
        cursors: dict[str, int] | None = None,
        *,
        execution_id: str | None = None,
    ) -> AgentEventBatch:
        raise NotImplementedError

    def read_result(self, execution_id: str, *, include_preview: bool = False) -> AgentResultRecord:
        raise NotImplementedError

    def control(self, execution_id: str, request: ControlRequest) -> ControlResult:
        raise NotImplementedError


class UnifiedAgentControl:
    def __init__(self, adapters: list[AgentControlAdapter] | tuple[AgentControlAdapter, ...]):
        self.adapters = tuple(adapters)
        self.last_errors: dict[str, str] = {}
        self._routes: dict[str, AgentControlAdapter] = {}

    def list_records(self, *, include_terminal: bool = False, engine: str | None = None) -> list[AgentRecord]:
        self.last_errors = {}
        self._routes = {}
        records: list[AgentRecord] = []
        for adapter in self.adapters:
            if engine is not None and adapter.engine != engine:
                continue
            try:
                rows = adapter.list_records(include_terminal=include_terminal)
            except Exception as exc:
                self._remember_error(adapter.engine, exc)
                continue
            for record in rows:
                if engine is not None and record.engine != engine:
                    continue
                records.append(record)
                self._routes[record.execution_id] = adapter
        records.sort(key=lambda record: record.execution_id)
        return records

    def get_record(self, execution_id: str) -> AgentRecord | None:
        adapter = self._routes.get(execution_id)
        if adapter is None:
            self.list_records(include_terminal=True)
            adapter = self._routes.get(execution_id)
        if adapter is None:
            return None
        try:
            record = adapter.get_record(execution_id)
        except Exception as exc:
            self._remember_error(adapter.engine, exc)
            return None
        if record is not None:
            self._routes[execution_id] = adapter
        return record

    def events_since(
        self,
        cursors: dict[str, int] | None = None,
        *,
        execution_id: str | None = None,
    ) -> AgentEventBatch:
        source_cursors = {str(key): int(value) for key, value in (cursors or {}).items()}
        events = []
        next_cursors: dict[str, int] = {}
        errors: dict[str, str] = {}
        seen_event_ids: set[str] = set()
        for adapter in self.adapters:
            try:
                batch = adapter.events_since(source_cursors, execution_id=execution_id)
            except Exception as exc:
                message = self._remember_error(adapter.engine, exc)
                errors[adapter.engine] = message
                continue
            for event in batch.events:
                if event.event_id and event.event_id in seen_event_ids:
                    continue
                if event.event_id:
                    seen_event_ids.add(event.event_id)
                events.append(event)
            for key, value in batch.next_cursors.items():
                next_cursors[key] = max(next_cursors.get(key, 0), int(value))
            for key, value in batch.errors.items():
                message = redact_sensitive_text(str(value))
                errors[key] = message
                self.last_errors[key] = message
        events.sort(key=lambda item: (item.source_cursor or "", item.source_sequence or item.sequence or 0, item.event_id or ""))
        return AgentEventBatch(events=tuple(events), next_cursors=next_cursors, errors=errors)

    def read_result(self, execution_id: str, *, include_preview: bool = False) -> AgentResultRecord:
        adapter = self._routes.get(execution_id)
        if adapter is None:
            self.list_records(include_terminal=True)
            adapter = self._routes.get(execution_id)
        if adapter is None:
            return AgentResultRecord(execution_id=execution_id, status="unknown", error="not_found")
        try:
            return adapter.read_result(execution_id, include_preview=include_preview)
        except Exception as exc:
            message = self._remember_error(adapter.engine, exc)
            return AgentResultRecord(execution_id=execution_id, status="unknown", error=message)

    def control(self, execution_id: str, request: ControlRequest) -> ControlResult:
        record = self.get_record(execution_id)
        if record is None:
            return ControlResult(ok=False, code="not_found", execution_id=execution_id)
        action = str(request.action or "")
        if not record.capabilities.supports_action(action):
            return ControlResult(
                ok=False,
                code="unsupported_capability",
                execution_id=execution_id,
                scope=record.record_kind,
                status=record.status,
                data={
                    "requestedAction": action,
                    "capabilities": sorted(record.capabilities.actions),
                },
            )
        adapter = self._routes.get(execution_id)
        if adapter is None:
            return ControlResult(ok=False, code="not_found", execution_id=execution_id)
        try:
            return adapter.control(execution_id, request)
        except Exception as exc:
            return ControlResult(
                ok=False,
                code="control_error",
                execution_id=execution_id,
                scope=record.record_kind,
                status=record.status,
                message=self._remember_error(adapter.engine, exc),
            )

    def _remember_error(self, engine: str, error: Exception) -> str:
        message = redact_sensitive_text(str(error))
        self.last_errors[str(engine)] = message
        return message
