import json, re, os, queue, threading
from dataclasses import dataclass
from typing import Any, Optional
@dataclass
class StepOutcome:
    data: Any
    next_prompt: Optional[str] = None
    should_exit: bool = False
def try_call_generator(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if hasattr(ret, '__iter__') and not isinstance(ret, (str, bytes, dict, list)): ret = yield from ret
    return ret

class BaseHandler:
    def tool_before_callback(self, tool_name, args, response): pass
    def tool_after_callback(self, tool_name, args, response, ret): pass
    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason): return next_prompt
    def dispatch(self, tool_name, args, response, index=0, tool_num=1):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args['_index'] = index; args['_tool_num'] = tool_num
            prer = yield from try_call_generator(self.tool_before_callback, tool_name, args, response)
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            _ = yield from try_call_generator(self.tool_after_callback, tool_name, args, response, ret)
            return ret
        elif tool_name == 'bad_json': return StepOutcome(None, next_prompt=args.get('msg', 'bad_json'), should_exit=False)
        else:
            yield f"未知工具: {tool_name}\n"
            return StepOutcome(None, next_prompt=f"未知工具 {tool_name}", should_exit=False)

def json_default(o): return list(o) if isinstance(o, set) else str(o)
def exhaust(g):
    try: 
        while True: next(g)
    except StopIteration as e: return e.value

def get_pretty_json(data):
    if isinstance(data, dict) and "script" in data:
        data = data.copy(); data["script"] = data["script"].replace("; ", ";\n  ")
    return json.dumps(data, indent=2, ensure_ascii=False).replace('\\n', '\n')

# Tools with no shared handler state and no process-global side effects can
# overlap. Everything else keeps the original strictly-ordered execution:
# browser control, code_run (it can chdir the process), writes, subagent
# control, and ask_user all depend on ordering or on exclusive access.
_PARALLEL_SAFE_TOOLS = frozenset({"file_read"})


def _tool_execution_mode(tool_name):
    """Return "parallel" for tools that are safe to run concurrently."""
    name = str(tool_name or "")
    if name.startswith("mcp__"):
        return "parallel"
    return "parallel" if name in _PARALLEL_SAFE_TOOLS else "sequential"


def _batch_tool_calls(tool_calls):
    """Group consecutive tool calls that share an execution mode."""
    batches = []
    for index, tc in enumerate(tool_calls):
        mode = _tool_execution_mode(tc.get("tool_name"))
        if batches and batches[-1][0] == mode:
            batches[-1][1].append((index, tc))
        else:
            batches.append((mode, [(index, tc)]))
    return batches


def _tool_header(tool_name, args, verbose):
    if tool_name == "no_tool":
        return None
    if verbose:
        return f"🔨 Tool: `{tool_name}`  📥 args:\n````text\n{get_pretty_json(args)}\n````\n"
    return f"🔨 {tool_name}({_compact_tool_args(tool_name, args)})\n\n\n"


def _dispatch_tool(handler, response, tool_name, args, index, tool_num):
    """Drive one dispatch generator and return (yielded_texts, outcome)."""
    gen = handler.dispatch(tool_name, args, response, index=index, tool_num=tool_num)
    if not hasattr(gen, "__next__"):
        return [], gen
    texts = []
    try:
        while True:
            chunk = next(gen)
            if chunk:
                texts.append(chunk)
    except StopIteration as e:
        return texts, e.value


def _run_single_tool(handler, response, tool_call, index, tool_num, verbose):
    """Run one tool call, streaming its output exactly as the old loop did.

    Verbose mode streams each chunk to the caller. Non-verbose mode discards
    the intermediate chunks and returns only the outcome, which is what the
    original ``exhaust(proxy())`` path did.
    """
    name = tool_call.get("tool_name")
    args = dict(tool_call.get("args") or {})
    header = _tool_header(name, args, verbose)
    if header:
        yield header
    gen = handler.dispatch(name, args, response, index=index, tool_num=tool_num)
    if not hasattr(gen, "__next__"):
        return gen
    if not verbose:
        return exhaust(gen)
    try:
        first = next(gen)
    except StopIteration as e:
        return e.value
    # The fence opens only once the generator has produced something, exactly
    # as in the original loop.
    yield "`````\n"
    yield first
    outcome = None
    while True:
        try:
            yield next(gen)
        except StopIteration as e:
            outcome = e.value
            break
    yield "`````\n"
    return outcome

def _run_parallel_tools(handler, response, batch, tool_num, verbose):
    """Run a batch of independent tool calls concurrently.

    Yields streamed tool output as it arrives. Returns one
    ``(index, tool_call, outcome)`` tuple per tool, in the original order, so
    the caller can use ``yield from`` and still get the results. Threads only
    ever run calls whose mode is "parallel", so no handler state is touched
    from two threads at once.
    """
    sink = queue.Queue()
    close = "`````\n"
    for index, tool_call in batch:
        header = _tool_header(tool_call.get("tool_name"), tool_call.get("args", {}), verbose)
        if header:
            yield header
        if verbose:
            yield close

    def worker(index, tool_call):
        name = tool_call.get("tool_name")
        args = dict(tool_call.get("args") or {})
        try:
            texts, outcome = _dispatch_tool(handler, response, name, args, index, tool_num)
            for chunk in texts:
                sink.put(("text", index, chunk))
            sink.put(("outcome", index, outcome))
        except BaseException as e:  # re-raised on the agent thread below
            sink.put(("error", index, e))

    threads = []
    for index, tool_call in batch:
        thread = threading.Thread(target=worker, args=(index, tool_call), name=f"ga-tool-{index}", daemon=True)
        thread.start()
        threads.append(thread)

    pending = {index for index, _ in batch}
    outcomes = {}; failures = {}
    while pending:
        kind, index, payload = sink.get()
        if kind == "text":
            if verbose:
                yield payload
        elif kind == "outcome":
            outcomes[index] = payload
            if verbose:
                yield close
            pending.discard(index)
        else:
            failures[index] = payload
            pending.discard(index)
    for thread in threads:
        thread.join(timeout=1.0)
    for index in sorted(failures):
        raise failures[index]
    # Returned (not yielded) so callers can use ``yield from`` and still receive
    # the outcome tuples; the strings above stream straight through.
    return [(index, tool_call, outcomes.get(index)) for index, tool_call in batch]

def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema, max_turns=40, verbose=True, initial_user_content=None):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_content if initial_user_content is not None else user_input}
    ]
    turn = 0;  handler.max_turns = max_turns
    while turn < handler.max_turns:
        turn += 1; turnstr = f'LLM Running (Turn {turn}) ...'
        if handler.parent.task_dir: turnstr = f'Turn {turn} ...'
        if verbose: turnstr = f'**{turnstr}**'
        yield f"\n\n{turnstr}\n\n"
        if turn%10 == 0: client.last_tools = ''  # 每10轮重置一次工具描述，避免上下文过大导致的模型性能下降
        response_gen = client.chat(messages=messages, tools=tools_schema)
        if verbose:
            response = yield from response_gen
            yield '\n\n'
        else:
            response = exhaust(response_gen)
            cleaned = _clean_content(response.content)
            if cleaned: yield cleaned + '\n'

        if not response.tool_calls: tool_calls = [{'tool_name': 'no_tool', 'args': {}}]
        else: tool_calls = [{'tool_name': tc.function.name, 'args': json.loads(tc.function.arguments), 'id': tc.id}
                          for tc in response.tool_calls]
       
        tool_results = []; next_prompts = set(); exit_reason = {}
        handler.current_turn = turn
        tool_num = len(tool_calls)
        for mode, batch in _batch_tool_calls(tool_calls):
            batch_outcomes = []
            if mode == 'parallel' and len(batch) > 1:
                # Independent calls (MCP tools, reads) overlap instead of
                # paying each other's latency one after another.
                for ii, tc, outcome in (yield from _run_parallel_tools(handler, response, batch, tool_num, verbose)):
                    batch_outcomes.append((ii, tc, outcome))
            else:
                for ii, tc in batch:
                    outcome = yield from _run_single_tool(handler, response, tc, ii, tool_num, verbose)
                    batch_outcomes.append((ii, tc, outcome))
            for ii, tc, outcome in batch_outcomes:
                tool_name, tid = tc.get('tool_name'), tc.get('id', '')
                if outcome is None:
                    continue
                if outcome.should_exit:
                    exit_reason = {'result': 'EXITED', 'data': outcome.data}; break
                if not outcome.next_prompt:
                    exit_reason = {'result': 'CURRENT_TASK_DONE', 'data': outcome.data}; break
                if outcome.next_prompt.startswith('未知工具'): client.last_tools = ''
                if outcome.data is not None and tool_name != 'no_tool':
                    datastr = json.dumps(outcome.data, ensure_ascii=False, default=json_default) if type(outcome.data) in [dict, list] else str(outcome.data)
                    tool_results.append({'tool_use_id': tid, 'content': datastr})
                next_prompts.add(outcome.next_prompt)
            if exit_reason:
                break
        if len(next_prompts) == 0 or exit_reason:
            if len(handler._done_hooks) == 0 or exit_reason.get('result', '') == 'EXITED': break
            next_prompts.add(handler._done_hooks.pop(0))
        next_prompt = handler.turn_end_callback(response, tool_calls, tool_results, turn, '\n'.join(next_prompts), exit_reason)
        messages = [{"role": "user", "content": next_prompt, "tool_results": tool_results}]   # just new message, history is kept in *Session
    if exit_reason: handler.turn_end_callback(response, tool_calls, tool_results, turn, '', exit_reason)
    return exit_reason or {'result': 'MAX_TURNS_EXCEEDED'}

def _clean_content(text):
    if not text: return ''
    def _shrink_code(m):
        lines = m.group(0).split('\n')
        lang = lines[0].replace('```','').strip()
        body = [l for l in lines[1:-1] if l.strip()]
        if len(body) <= 6: return m.group(0)
        preview = '\n'.join(body[:5])
        return f'```{lang}\n{preview}\n  ... ({len(body)} lines)\n```'
    text = re.sub(r'```[\s\S]*?```', _shrink_code, text)
    for p in [r'<file_content>[\s\S]*?</file_content>', r'<tool_(?:use|call)>[\s\S]*?</tool_(?:use|call)>', r'(\r?\n){3,}']:
        text = re.sub(p, '\n\n' if '\\n' in p else '', text)
    return text.strip()

def _compact_tool_args(name, args):
    a = {k: v for k, v in args.items() if k != '_index'}
    for k in ('path',): 
        if k in a: a[k] = os.path.basename(a[k])
    if name == 'update_working_checkpoint': s = a.get('key_info', ''); return (s[:60]+'...') if len(s)>60 else s
    if name == 'ask_user':
        q = str(a.get('question', ''))
        cs = a.get('candidates') or []
        if cs: q += '\ncandidates:\n' + '\n'.join(f'- {c}' for c in cs)
        return q
    s = json.dumps(a, ensure_ascii=False); return (s[:120]+'...') if len(s)>120 else s
