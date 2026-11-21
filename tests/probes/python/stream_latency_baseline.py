"""Replay fake provider text through the real drain and JSONL display consumer.

Root cause: genericagent/core/app.py:1889 holds short text until 16 chars.
Determinism: capture which prefixes were enqueued while the generator was still
suspended, and end with a known sentinel. Wall-clock samples are descriptive.
Keep this to distinguish text held by the producer from consumer/JSONL latency.
Frame mode requires the candidate bridge context-usage sampling implementation.
Run from the repository root: python -m tests.probes.python.stream_latency_baseline
"""

import argparse
import io
import json
import os
import queue
import time
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch


def replay(chunks, *, mode, gaps=None, track_prefixes=True, timeout=15, backend=None):
    from frontends.ink.bridge import GenericAgentBridge, make_stdout_emitter
    from genericagent.core.app import GenericAgent

    marks = {}
    prefixes = []
    enqueued = []
    produced = []
    output = io.StringIO()
    emit_jsonl = make_stdout_emitter(output)
    agent = GenericAgent.__new__(GenericAgent)
    agent.task_dir = None
    agent.stop_sig = False
    agent.inc_out = True
    if backend is not None:
        agent.llmclient = SimpleNamespace(backend=backend)

    class DisplayQueue(queue.Queue):
        def put(self, item):
            if item is not None and 'next' in item:
                marks.setdefault('python_display_enqueue', time.perf_counter_ns())
                if track_prefixes:
                    enqueued.append(item['next'])
            super().put(item)

        def get(self):
            try:
                item = super().get(timeout=timeout)
            except queue.Empty as exc:
                raise AssertionError('timed out waiting for display queue output') from exc
            if item is None:
                raise AssertionError('fake stream failed before done')
            return item

    def generate():
        for index, chunk in enumerate(chunks):
            if gaps is not None and gaps[index]:
                time.sleep(gaps[index])
            if chunk:
                marks.setdefault('provider_first_chunk', time.perf_counter_ns())
            produced.append(chunk)
            yield chunk
            if track_prefixes:
                prefixes.append(''.join(enqueued))
        return {'result': 'CURRENT_TASK_DONE'}

    display = DisplayQueue()
    errors = []

    def produce():
        try:
            text, outcome = agent._drain_agent_stream(generate(), display, 'probe')
            assert outcome['kind'] == 'completed', outcome
            display.put({'done': text})
        except Exception as exc:
            errors.append(exc)
            display.put(None)

    def emit(event):
        emit_jsonl(event)
        stage = {'assistant_delta': 'bridge_emit', 'assistant_done': 'assistant_done'}.get(event['type'])
        if stage:
            marks.setdefault(stage, time.perf_counter_ns())

    # No bridge constructor, agent factory, provider configuration or real tools.
    bridge = SimpleNamespace(agent=agent, emit=emit, _emit_agent_read_model=lambda: None,
                             _start_queued_turn_after_end=lambda: None)
    for name in ('_backend_token_usage', '_emit_context_usage_if_changed', '_await_agent_idle'):
        setattr(bridge, name, getattr(GenericAgentBridge, name).__get__(bridge))

    with patch.dict(os.environ, {'GA_STREAM_SCHEDULER': mode}):
        producer = Thread(target=produce, daemon=True)
        producer.start()
        GenericAgentBridge._consume_display_queue(bridge, 1, display)
        producer.join(timeout=timeout)
        assert not producer.is_alive(), 'timed out waiting for fake stream producer'
    if errors:
        raise errors[0]

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert not any(event['type'] == 'error' for event in events), events
    deltas = [event['text'] for event in events if event['type'] == 'assistant_delta']
    expected = ''.join(produced)
    assert deltas, 'no assistant delta from the display consumer'
    assert ''.join(deltas) == expected, 'incremental text lost, duplicated or reordered'
    assert [event['text'] for event in events if event['type'] == 'assistant_done'] == [expected]
    stages = ('provider_first_chunk', 'python_display_enqueue', 'bridge_emit', 'assistant_done')
    stamp = [marks[stage] for stage in stages]
    assert stamp == sorted(stamp), 'display stages are out of order'
    result = {'prefixes': prefixes, 'delta_count': len(deltas), 'text_chars': len(expected),
              'first_ms': {stage: round((marks[stage] - stamp[0]) / 1e6, 3) for stage in stages}}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('legacy', 'frame'), default='legacy')
    parser.add_argument('--burst', action='store_true', help='Replay 512 short chunks against a long history')
    parser.add_argument('--rounds', type=int, default=5)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be positive')
    chunks = ('甲', '乙' * 14, '丙', '丁')
    backend = None
    if args.burst:
        chunks = ('甲',) * 512
        backend = SimpleNamespace(history=[{'role': 'user', 'content': 'fixture ' * 50}] * 1000,
                                  last_usage_tokens=None, auto_compact_tokens=400000, hard_limit_tokens=410000)
    for _ in range(args.rounds):
        row = replay(chunks, mode=args.mode, gaps=None if args.burst else (0, .010, .060, .010),
                     track_prefixes=not args.burst, backend=backend)
        prefixes = row.pop('prefixes')
        if not args.burst:
            assert prefixes[0] == '', args.mode
        print(json.dumps(row))
    print(f'✔ {args.mode}: verified output and event ordering in {args.rounds}/{args.rounds} rounds')


if __name__ == '__main__':
    main()
