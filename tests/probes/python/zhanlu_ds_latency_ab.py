"""Repeat GA's display-queue comparison with live DeepSeek 4.1 Flash output.

Measures parsed text to enqueue at core/app.py:1889 and to JSONL emission
at frontends/ink/bridge.py:3887. Both modes retain Python batching. Run the
comparison with the candidate bridge context-usage sampling implementation.
Ink scheduling has a separate offline probe. Fixed synthetic prompts
and alternating AB/BA order control the workload; live timings still vary.
Keep this probe to separate provider latency from local buffering.
Run from the root with --real to use the project profile. No tools mounted.
"""

import argparse
import contextlib
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from genericagent.llm.config import load_llm_config
from tests.probes.python.stream_latency_baseline import replay

PROFILE = 'deep'
MODEL = 'moma/deepseek-v4.1-flash'
ROOT = Path(__file__).resolve().parents[3]
TASKS = (
    ('moon', '用中文简洁解释为什么月亮会出现月相。正文最后另起一行写 LATENCY-MOON-END。', 'LATENCY-MOON-END'),
    ('search', '用中文解释二分查找的前提、步骤、循环不变量和复杂度，给一个具体例子。正文最后另起一行写 LATENCY-SEARCH-END。', 'LATENCY-SEARCH-END'),
)


def resolve_profile(path, name, *, load=load_llm_config):
    if name != PROFILE:
        raise ValueError('only the approved profile deep is allowed')
    config = load(str(path))
    resolved = config.resolve(name)
    wire = resolved.to_legacy_cfg()
    endpoint = urlsplit(wire['apibase'])
    if (resolved.provider_key != 'ecloud' or wire['model'] != MODEL or resolved.wire_api != 'openai_chat' or not wire['apikey']
            or endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password):
        raise ValueError('approved profile must resolve to authenticated DeepSeek 4.1 Flash over HTTPS')
    return config


def comparison_order(rounds):
    for index in range(1, rounds + 1):
        modes = ('legacy', 'candidate') if index % 2 else ('candidate', 'legacy')
        for mode in modes:
            yield index, mode


def summarize(rows):
    result = {}
    for mode in ('legacy', 'candidate'):
        group = [row for row in rows if row['mode'] == mode]
        if not group:
            continue
        keys = {name for row in group if row['ok'] for name in row['ms']}
        stats = {}
        for key in sorted(keys):
            ordered = sorted(row['ms'][key] for row in group if row['ok'] and key in row['ms'])
            stats[key] = {f'p{p}': ordered[math.ceil(p / 100 * len(ordered)) - 1] for p in (50, 90)}
        result[mode] = {'runs': len(group), 'passed': sum(row['ok'] for row in group), 'ms': stats}
    return result


def trial(session, prompt, marker, mode):
    from genericagent.llm import core

    clock = time.perf_counter_ns
    session.history = []
    session.last_usage_tokens = None
    session.reset_cancel()
    transport = core._get_http_session(session)
    post = transport.post
    request_starts = []
    output = []
    first_text = last_text = None

    def timed_post(*args, **kwargs):
        request_starts.append(clock())
        response = post(*args, **kwargs, allow_redirects=False)
        if 300 <= response.status_code < 400:
            response.close()
            raise ValueError('redirects disabled for benchmark')
        return response

    def chunks():
        nonlocal first_text, last_text
        for text in session.ask({'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}):
            if text:
                last_text = clock()
                if first_text is None:
                    first_text = last_text
                output.append(text)
                yield text

    submitted = clock()
    with patch.object(transport, 'post', timed_post):
        result = replay(chunks(), mode='frame' if mode == 'candidate' else 'legacy', track_prefixes=False, timeout=90, backend=session)
    assert request_starts and first_text is not None and last_text is not None
    usage = session.last_usage_tokens or {}
    stamps = result['first_ms']

    def elapsed(start, stop):
        return round((stop - start) / 1e6, 3)

    answer = ''.join(output)
    return {
        'ok': len(answer) > len(marker) and answer.strip().rstrip('。').endswith(marker) and '!!!' not in answer,
        'requests': len(request_starts),
        'ms': {
            'submit_to_request': elapsed(submitted, request_starts[0]),
            'request_to_text': elapsed(request_starts[0], first_text),
            'text_to_enqueue': stamps['python_display_enqueue'],
            'text_to_bridge': stamps['bridge_emit'],
            'text_to_last_text': elapsed(first_text, last_text),
            'submit_to_done': round(elapsed(submitted, first_text) + stamps['assistant_done'], 3),
        },
        'delta_count': result['delta_count'],
        'usage': {key: value for key, value in usage.items() if key in {
            'input_tokens', 'output_tokens', 'cached_tokens', 'cache_read_tokens', 'total_tokens',
        } and isinstance(value, int)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=(PROFILE,), default=PROFILE)
    parser.add_argument('--rounds', type=int, default=3, choices=range(1, 11), metavar='1..10')
    parser.add_argument('--real', action='store_true')
    args = parser.parse_args()
    if not args.real:
        print(json.dumps({'profile': PROFILE, 'tasks': [task[0] for task in TASKS],
                          'order': list(comparison_order(args.rounds)), 'offline': True}))
        return 0

    from genericagent.llm.client import build_session
    from genericagent.llm.core import close_http_session

    try:
        config = resolve_profile(ROOT / 'llm.yaml', args.profile)
        session = build_session(config, args.profile)
        if not session.stream:
            raise ValueError('profile must have streaming enabled')
        session.system = 'You are a helpful assistant.'
        session.max_retries = 0
    except Exception as exc:
        print(json.dumps({'profile': args.profile, 'setup_error': type(exc).__name__}), flush=True)
        return 1
    print(json.dumps({'profile': PROFILE, 'model': MODEL, 'wire': session.api_mode,
                      'reasoning_effort': session.reasoning_effort}), flush=True)
    all_passed = True
    try:
        for task_id, prompt, marker in TASKS:
            rows = []
            for index, mode in comparison_order(args.rounds):
                try:
                    with open(os.devnull, 'w', encoding='utf-8') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                        row = trial(session, prompt, marker, mode)
                except Exception as exc:
                    row = {'ok': False, 'failure': type(exc).__name__, 'ms': {}}
                row.update(task=task_id, round=index, mode=mode)
                print(json.dumps(row), flush=True)
                rows.append(row)
                if 'failure' in row:
                    return 1
            print(json.dumps({'task': task_id, 'summary': summarize(rows)}), flush=True)
            all_passed &= all(row['ok'] for row in rows)
    finally:
        close_http_session(session)
    return 0 if all_passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
