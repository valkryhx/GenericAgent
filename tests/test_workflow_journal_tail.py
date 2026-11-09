"""Performance guard for ``WorkflowStore.append_event``.

The store re-read the whole journal on every append just to learn the next
sequence, so writing n events cost O(n^2): 2000 events spent ~31s inside the
store, and the cost grew for every long workflow. See
``docs/20261109-GA-workflow启动卡顿与journal性能分析.md``.

The guard counts bytes instead of seconds: timing on this machine is dominated
by ``open()`` latency (Windows Defender), so a wall-clock threshold would be
flaky while still failing to distinguish O(1) tail reads from O(n) full reads.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from genericagent.workflow.models import WorkflowEvent, WorkflowRun

TAIL_WINDOW_BYTES = 64 * 1024


def _new_store(td):
    from genericagent.workflow.store import WorkflowStore

    return WorkflowStore(Path(td))


def _journal(store, run):
    return Path(run.artifact_dir) / "journal.jsonl"


def _write_history(journal, run_id, count):
    journal.write_text(
        "".join(
            json.dumps({"type": "agent_log", "runId": run_id, "sessionId": "session_a", "sequence": index + 1, "payload": {}})
            + "\n"
            for index in range(count)
        ),
        encoding="utf-8",
    )


class _CountingFile:
    def __init__(self, handle, counter):
        self._handle = handle
        self._counter = counter

    def read(self, *args):
        data = self._handle.read(*args)
        self._counter["bytes"] += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False


class _CountingPath:
    """Duck-typed stand-in for ``Path`` that measures how much the tail read touches."""

    def __init__(self, path):
        self.path = Path(path)
        self.counts = {"bytes": 0, "full": 0}

    def open(self, mode="r", *args, **kwargs):
        return _CountingFile(self.path.open(mode, *args, **kwargs), self.counts)

    def read_text(self, *args, **kwargs):
        self.counts["full"] += 1
        return self.path.read_text(*args, **kwargs)


class AppendEventTailTest(unittest.TestCase):
    def _run(self, store, run_id="wf_1794000000-dddd"):
        return store.create_run(WorkflowRun(session_id="session_a", script="return 1;", run_id=run_id))

    def test_tail_sequence_reads_a_bounded_window_no_matter_the_journal_length(self):
        from genericagent.workflow.store import _tail_sequence

        with tempfile.TemporaryDirectory() as td:
            store = _new_store(td)
            run = self._run(store)
            _write_history(_journal(store, run), run.run_id, 5000)

            counting = _CountingPath(_journal(store, run))
            self.assertEqual(5000, _tail_sequence(counting))

            self.assertLessEqual(counting.counts["bytes"], TAIL_WINDOW_BYTES + 8)
            self.assertEqual(0, counting.counts["full"], "the whole journal must never be read")

    def test_appending_uses_the_tail_path_and_keeps_sequences_strictly_increasing(self):
        from genericagent.workflow import store as store_module

        with tempfile.TemporaryDirectory() as td:
            store = _new_store(td)
            run = self._run(store)
            _write_history(_journal(store, run), run.run_id, 5000)

            with mock.patch.object(store_module, "_tail_sequence", wraps=store_module._tail_sequence) as spy:
                event = store.append_event(
                    run,
                    WorkflowEvent(run_id=run.run_id, session_id="session_a", event_type="agent_log", sequence=0, payload={"i": 5000}),
                )

            self.assertEqual(1, spy.call_count)
            self.assertEqual(5001, event.sequence)

    def test_two_writers_keep_sequences_unique_after_the_tail_read(self):
        with tempfile.TemporaryDirectory() as td:
            first = _new_store(td)
            second = _new_store(td)
            run = self._run(first, "wf_1794000000-eeee")

            sequences = []
            for store in (first, second, first, second):
                event = store.append_event(
                    run,
                    WorkflowEvent(run_id=run.run_id, session_id="session_a", event_type="agent_log", sequence=0, payload={}),
                )
                sequences.append(event.sequence)

            self.assertEqual([1, 2, 3, 4], sequences)


if __name__ == "__main__":
    unittest.main()
