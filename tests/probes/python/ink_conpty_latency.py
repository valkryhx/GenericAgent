"""Measure production Ink writes via Windows ConPTY and pyte screen replay.

App.tsx:1572 schedules bridge deltas ahead of Ink's own throttle. The offline
control sends identical lines at fixed intervals and gates on visible text plus
bridge idle. Keep this to measure both schedulers with normal terminal writes,
including cursor parking. Real runs vary and use only the approved deep profile.

From the root, with pywinpty, pyte and Ink dependencies installed:
  python -m tests.probes.python.ink_conpty_latency [--real] [--rounds 3]
"""

import argparse
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from statistics import median

from tests.probes.python.zhanlu_ds_latency_ab import MODEL, PROFILE, ROOT, comparison_order, resolve_profile

ROWS, COLS = 42, 120
MARKER = 'LATENCYCOMPLETE'
LINE = re.compile(r'\bR0([1-6]) +([A-Za-z][^\r\n]*)')
PROMPT = ('Explain binary search in English with exactly six short lines. At the start of each line write '
          'R followed by its two-digit number, then a space and the explanation. No Markdown or tools. '
          'On a separate final line write LATENCY and COMPLETE joined as a single word.')
FIXTURE = (
    'R01 Start with a sorted array.',
    'R02 Maintain an inclusive search range.',
    'R03 Compare the middle value with the key.',
    'R04 Discard the half that cannot contain the key.',
    'R05 Repeat until found or until the range is empty.',
    'R06 The running time is logarithmic.',
    MARKER,
)


def bridge_main():
    import requests
    from frontends.ink import bridge
    from genericagent.core import app
    from genericagent.llm import core

    real = os.environ['GA_CONPTY_LATENCY_REAL'] == '1'
    resolved = resolve_profile(ROOT / 'llm.yaml', PROFILE).resolve(PROFILE) if real else None
    expected_effort = resolved.reasoning_effort if resolved else None
    expected_url = (core.auto_make_url(resolved.to_legacy_cfg()['apibase'], 'chat/completions')
                    if resolved else 'https://example.invalid/v1/chat/completions')
    journal = []
    trace = Path(os.environ['GA_CONPTY_LATENCY_TRACE'])

    def mark(stage, **counts):
        journal.append(dict(stage=stage, ns=time.perf_counter_ns(), **counts))

    parser = core._parse_openai_sse

    def track_parser(*args, **kwargs):
        gen = parser(*args, **kwargs)
        while True:
            try:
                chunk = next(gen)
            except StopIteration as end:
                if any(block.get('type') == 'tool_use' for block in end.value or []):
                    raise ValueError('unexpected tool call in text-only benchmark')
                return end.value
            if chunk:
                mark('provider_text', chars=len(chunk))
            yield chunk

    class FixtureResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def iter_lines(self):
            time.sleep(.14)
            for index, line in enumerate(FIXTURE):
                if index:
                    time.sleep(.045)
                yield ('data: ' + json.dumps({'choices': [{'delta': {'content': line + '\n'}}]})).encode()
            yield b'data: [DONE]'

    post = requests.Session.post

    def track_post(session, url, **kwargs):
        payload = kwargs['json']
        if (url != expected_url or payload['model'] != MODEL or payload.get('tools')
                or not payload.get('stream') or payload.get('reasoning_effort') != expected_effort):
            raise ValueError('unapproved request in text-only benchmark')
        if any(row['stage'] == 'request' for row in journal):
            raise ValueError('more than one request in a single-turn benchmark')
        mark('request', effort=payload.get('reasoning_effort'))
        if not real:
            return FixtureResponse()
        response = post(session, url, **kwargs, allow_redirects=False)
        if 300 <= response.status_code < 400:
            response.close()
            raise ValueError('redirects disabled for benchmark')
        return response

    emitter = bridge.make_stdout_emitter

    def track_emitter(stdout):
        emit = emitter(stdout)

        def track(event):
            kind = event['type']
            if kind == 'ready':
                mark('ready')
            elif kind == 'assistant_delta':
                text = event.get('text', '')
                mark('banner' if 'LLM Running (Step' in text else 'bridge_delta', chars=len(text))
            elif kind == 'assistant_done':
                text = event.get('text', '')
                mark('done', complete=MARKER in text and '!!!' not in text, lines=len(LINE.findall(text)))
            elif kind == 'token_usage':
                mark('usage', **{k: event[k] for k in ('inputTokens', 'outputTokens', 'cachedTokens') if k in event})
            elif kind == 'error':
                mark('error')
            emit(event)
            if kind == 'ready':
                trace.with_suffix('.ready').touch()
            if kind == 'status' and event.get('status') == 'idle' and event.get('taskId'):
                mark('idle')
                # Publish once to a new file. On Windows the reader can block
                # replacement of an existing file it currently has open.
                pending = trace.with_suffix('.tmp')
                pending.write_text(json.dumps(journal), encoding='utf-8')
                pending.replace(trace)

        return track

    chat = core.NativeToolClient.chat

    def text_only(self, messages, tools=None):
        if self.backend.name != PROFILE:
            raise ValueError('unexpected active profile in benchmark')
        # /model carries the previous session's effort when the target supports it.
        self.backend.reasoning_effort = expected_effort
        self.backend.max_retries = 0
        return chat(self, messages, tools=None)

    bridge.make_stdout_emitter = track_emitter
    requests.Session.post = track_post
    core._parse_openai_sse = track_parser
    core._write_llm_log = lambda *args, **kwargs: None
    core.NativeToolClient.chat = text_only
    app.build_system_prompt_sections = lambda *_: {'base': 'Follow the requested plain text format.'}
    return bridge.main([])


def run_trial(mode, real, trial_dir, *, trace_writes=False):
    from winpty import Backend, PtyProcess
    import pyte

    trial_dir.mkdir(parents=True)
    trace = trial_dir / 'trace.json'
    config = ROOT / 'llm.yaml'
    if not real:
        config = trial_dir / 'fixture.json'
        config.write_text(json.dumps({
            'providers': {'ecloud': {'base_url': 'https://example.invalid/v1',
                                     'api_key': 'fixture', 'wire_api': 'openai_chat'}},
            'models': {'flash': {'provider': 'ecloud', 'api_model': MODEL}},
            'profiles': {PROFILE: {'model': 'flash'}}, 'active_profile': PROFILE,
        }), encoding='utf-8')
    env = dict(os.environ, GA_CONPTY_LATENCY_REAL=str(int(real)), GA_CONPTY_LATENCY_TRACE=str(trace),
               GA_RUN_REAL_API_E2E=str(int(real)), GA_LLM_CONFIG=str(config), GA_PROJECT_ROOT=str(trial_dir),
               GA_CONFIG_DIR=str(trial_dir), GA_DATA_DIR=str(trial_dir / 'data'),
               GA_MCP_CONFIG=str(trial_dir / 'missing-mcp.json'), GA_HOOKS_DISABLED='1', GA_GOAL_WAKE='0',
               GA_SCHEMA_CACHE='off', GA_HISTORY_PROJECTION='full', GA_LIVE_TRANSCRIPT_RENDER='legacy',
               GA_STREAM_SCHEDULER='frame' if mode == 'candidate' else 'legacy', TERM='xterm-256color',
               PYTHONPATH=str(ROOT), PYTHONUTF8='1')
    env.pop('CI', None)
    write_trace = trial_dir / 'writes.jsonl' if trace_writes else None
    if write_trace is not None:
        if real:
            raise ValueError('stdout tracing is available for offline fixtures only')
        preload = ROOT / 'frontends' / 'ink' / 'ui' / 'probes' / 'conpty_write_trace.cjs'
        env['GA_CONPTY_WRITE_TRACE'] = str(write_trace)
        env['NODE_OPTIONS'] = (env.get('NODE_OPTIONS', '') + f' --require="{preload.as_posix()}"').strip()
    command = [sys.executable, '-m', 'ga_cli', 'ink', '--', '--python', sys.executable, '--bridge', str(Path(__file__).resolve())]
    proc = PtyProcess.spawn(command, dimensions=(ROWS, COLS), env=env, cwd=str(ROOT), backend=Backend.ConPTY)
    chunks = queue.Queue()
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)

    def reader():
        try:
            while proc.isalive():
                text = proc.read(65536)
                if text:
                    chunks.put((time.perf_counter_ns(), text))
        except (EOFError, OSError):
            pass
        finally:
            chunks.put((time.perf_counter_ns(), None))

    def observe(timeout, label, check):
        deadline = time.monotonic() + timeout
        at = time.perf_counter_ns()
        while time.monotonic() < deadline:
            try:
                at, text = chunks.get(timeout=min(.05, max(0, deadline - time.monotonic())))
            except queue.Empty:
                text = ''
            if text is None:
                raise RuntimeError(f'GA exited while waiting for {label}')
            if text:
                stream.feed(text)
            if check(at, chr(10).join(screen.display)):
                return
        if not real:
            (trial_dir / 'screen.txt').write_text(chr(10).join(screen.display), encoding='utf-8', newline=chr(10))
        raise TimeoutError(f'timed out after {timeout}s waiting for {label}')

    threading.Thread(target=reader, daemon=True).start()
    try:
        observe(20, 'composer and bridge ready', lambda _, view: trace.with_suffix('.ready').is_file() and any(
            line.lstrip().startswith('>') for line in view.splitlines()))
        # A command and Enter in one input burst can be treated as a multiline paste.
        proc.write('/model deep')
        observe(5, 'model command in composer', lambda _, view: '/model deep' in view)
        proc.write(chr(13))
        observe(20, 'approved model in the UI', lambda _, view: f'Set model to {PROFILE}/{MODEL}' in view)
        proc.write(PROMPT)
        observe(5, 'prompt in composer', lambda _, view: 'joined as a single word.' in view)
        submitted = time.perf_counter_ns()
        proc.write(chr(13))
        return measure_trial(observe, trace, submitted, mode, real, write_trace)
    finally:
        try:
            if proc.isalive():
                proc.write('/quit')
                observe(2, 'quit command in composer', lambda _, view: '/quit' in view)
                proc.write(chr(13))
                observe(4, 'quit', lambda *_: not proc.isalive())
        except (EOFError, OSError, RuntimeError):
            pass
        finally:
            proc.close(force=True)


def measure_trial(observe, trace, submitted, mode, real, write_trace=None):
    visible = {}
    reveals = []
    longest = {}
    final_at = None

    def check(at, view):
        nonlocal final_at
        changed = False
        for label, line in LINE.findall(view):
            visible.setdefault(label, at)
            if len(line.rstrip()) > longest.get(label, 0):
                longest[label] = len(line.rstrip())
                changed = True
        if changed:
            reveals.append(at)
        if MARKER in view and final_at is None:
            final_at = at
        return final_at is not None and trace.is_file() and (write_trace is None or write_trace.is_file())

    observe(45, 'final terminal marker and bridge idle', check)
    marks = {}
    for row in read_trace(trace):
        marks.setdefault(row['stage'], []).append(row)
    assert len(marks.get('request', [])) == 1 and 'error' not in marks, 'one successful request required'
    assert set(visible) == set('123456'), 'six numbered lines must reach the visible screen'
    assert marks['done'][0]['complete'] and marks['done'][0]['lines'] == 6, 'incomplete or duplicated final text'
    if not real:
        assert len(reveals) >= 2, 'fixture never visibly streamed'
    request = marks['request'][0]['ns']
    first = marks['provider_text'][0]['ns']
    bridge_first = marks['bridge_delta'][0]['ns']
    assert submitted <= request <= first <= bridge_first <= visible['1'] <= final_at, 'stages out of order'
    gaps = [(end - start) / 1e6 for start, end in zip(reveals, reveals[1:])]
    usage = {k: v for k, v in marks.get('usage', [{}])[-1].items() if k.endswith('Tokens')}
    result = dict(mode=mode, real=real, ok=True, reveals=len(reveals), delta_count=len(marks['bridge_delta']), usage=usage,
                effort=marks['request'][0]['effort'],
                ms=dict(submit_to_first=(visible['1'] - submitted) / 1e6,
                        request_to_text=(first - request) / 1e6, text_to_first=(visible['1'] - first) / 1e6,
                        bridge_to_first=(visible['1'] - bridge_first) / 1e6,
                        submit_to_final=(final_at - submitted) / 1e6, max_reveal_gap=max(gaps, default=None)))
    if write_trace is not None:
        writes = {}
        for line in write_trace.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            for label in row['labels']:
                writes.setdefault(label, int(row['ns']))
        assert writes.keys() == visible.keys(), 'each fixture line must have a stdout timing'
        assert all(writes[label] <= visible[label] for label in writes), 'stdout timestamps must precede visibility'
        def max_gap(times):
            return max(b - a for a, b in zip(times, times[1:])) / 1e6
        labels = sorted(visible)
        result['ms'].update(
            first_write_to_screen=(visible['1'] - writes['1']) / 1e6,
            max_write_gap=max_gap([writes[label] for label in labels]),
            max_line_screen_gap=max_gap([visible[label] for label in labels]),
        )
    return result


def read_trace(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--trace-writes', action='store_true', help='Time offline fixture writes before ConPTY')
    parser.add_argument('--rounds', type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    if args.real and args.trace_writes:
        parser.error('--trace-writes is offline only')
    if args.real:
        resolve_profile(ROOT / 'llm.yaml', PROFILE)
    root = ROOT / 'tests' / 'probes' / 'captures'
    root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='ink-conpty-latency-', dir=root))
    rows = []
    print(json.dumps({'capture': str(output.relative_to(ROOT)), 'real': args.real}), flush=True)
    with (output / 'results.jsonl').open('w', encoding='utf-8', newline=chr(10)) as results:
        for round_no, mode in comparison_order(args.rounds):
            row = run_trial(mode, args.real, output / f'{round_no}-{mode}', trace_writes=args.trace_writes)
            row['round'] = round_no
            rows.append(row)
            line = json.dumps(row)
            results.write(line + chr(10))
            results.flush()
            print(line, flush=True)
    for mode in ('legacy', 'candidate'):
        sample = [row for row in rows if row['mode'] == mode]
        for metric in sample[0]['ms']:
            values = [row['ms'][metric] for row in sample if row['ms'][metric] is not None]
            if values:
                print(f'{mode} {metric} ms p50={median(values):.2f} max={max(values):.2f}')
    print(f'✔ checked model, visible output, event order and completion in {len(rows)}/{len(rows)} trials')
    return 0


if __name__ == '__main__':
    raise SystemExit(bridge_main() if 'GA_CONPTY_LATENCY_TRACE' in os.environ else main())
