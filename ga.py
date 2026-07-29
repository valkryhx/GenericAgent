import sys, os, re, json, time, threading, importlib
from datetime import datetime
from pathlib import Path
import tempfile, traceback, subprocess, itertools, collections, difflib
def _configure_stdio_utf8():
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(stream, 'reconfigure'):
            try: stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception: stream.reconfigure(errors='replace')
_configure_stdio_utf8()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agent_loop import BaseHandler, StepOutcome, json_default, try_call_generator
script_dir = os.path.dirname(os.path.abspath(__file__))


def _stop_requested(stop_signal):
    if stop_signal is None:
        return False
    is_set = getattr(stop_signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else bool(stop_signal)

def code_run(code, code_type="python", timeout=60, cwd=None, code_cwd=None, stop_signal=None, maxlen=10000):
    """代码执行器
    python: 运行复杂的 .py 脚本（文件模式）
    powershell/bash: 运行单行指令（命令模式）
    优先使用python，仅在必要系统操作时使用powershell"""
    preview = (code[:60].replace('\n', ' ') + '...') if len(code) > 60 else code.strip()
    yield f"[Action] Running {code_type} in {os.path.basename(cwd)}: {preview}\n"
    cwd = cwd or os.path.join(script_dir, 'temp'); tmp_path = None
    if code_type in ["python", "py"]:
        if code_cwd is not None and not os.path.isdir(code_cwd):
            return {"status": "error", "msg": f"code_cwd does not exist: {code_cwd}"}
        tmp_file = tempfile.NamedTemporaryFile(suffix=".ai.py", delete=False, mode='w', encoding='utf-8', dir=code_cwd)
        cr_header = os.path.join(script_dir, 'assets', 'code_run_header.py')
        if os.path.exists(cr_header):
            with open(cr_header, encoding='utf-8') as header:
                tmp_file.write(header.read())
        tmp_file.write(code)
        tmp_path = tmp_file.name
        tmp_file.close()
        cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]   
    elif code_type in ["powershell", "bash", "sh", "shell", "ps1", "pwsh"]:
        if os.name == 'nt': cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]
        else: cmd = ["bash", "-c", code]
    else:
        return {"status": "error", "msg": f"不支持的类型: {code_type}"}
    print("code run output:") 
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0 # SW_HIDE
    full_stdout = []

    def stream_reader(proc, logs):
        try:
            for line_bytes in iter(proc.stdout.readline, b''):
                try: line = line_bytes.decode('utf-8')
                except UnicodeDecodeError: line = line_bytes.decode('gbk', errors='ignore')
                logs.append(line)
                try: print(line, end="") 
                except: pass
        except: pass

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0, cwd=cwd, startupinfo=startupinfo,
            creationflags=0x08000000 if os.name == 'nt' else 0
        )
        start_t = time.time()
        t = threading.Thread(target=stream_reader, args=(process, full_stdout), daemon=True)
        t.start()

        while process.poll() is None:
            istimeout = time.time() - start_t > timeout
            if istimeout or _stop_requested(stop_signal):
                process.kill()
                print("[Debug] Process killed due to timeout or stop signal.")
                if istimeout: full_stdout.append("\n[Timeout Error] 超时强制终止")
                else: full_stdout.append("\n[Stopped] 用户强制终止")
                break
            time.sleep(0.1)

        t.join(timeout=1)
        exit_code = process.poll()

        stdout_str = "".join(full_stdout)
        status = "success" if exit_code == 0 else "error"
        output_snippet = smart_format(stdout_str, max_str_len=600, omit_str='\n\n[omitted long output]\n\n')
        output_snippet = re.sub(r'`{4,}', lambda m: m.group(0)[:3] + '\u200b' + m.group(0)[3:], output_snippet)
        yield f"[Status] Exit Code: {exit_code}\n[Stdout]\n{output_snippet}\n"
        if process.stdout: threading.Thread(target=process.stdout.close, daemon=True).start()
        return {
            "status": status,
            "stdout": smart_format(stdout_str, max_str_len=maxlen, omit_str='\n\n[omitted long output]\n\n'),
            "exit_code": exit_code
        }
    except Exception as e:
        if 'process' in locals(): process.kill()
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type in ["python", "py"] and tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)


def ask_user(question, candidates=None):
    """question: 向用户提出的问题。candidates: 可选的候选项列表"""
    return {"status": "INTERRUPT", "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []}}

import simphtml
driver = None
def first_init_driver():
    global driver
    from TMWebDriver import TMWebDriver
    driver = TMWebDriver()
    for i in range(20):
        time.sleep(1)
        sess = driver.get_all_sessions()
        if len(sess) > 0: break
    if len(sess) == 0: return 
    if len(sess) == 1: 
        #driver.newtab()
        time.sleep(3)

def web_scan(tabs_only=False, switch_tab_id=None, text_only=False, maxlen=35000):
    """获取当前页面的简化HTML内容和标签页列表。注意：简化过程会过滤边栏、浮动元素等非主体内容。
    tabs_only: 仅返回标签页列表，不获取HTML内容（节省token）。
    switch_tab_id: 可选参数，如果提供，则在扫描前切换到该标签页。
    应当多用execute_js，少全量观察html"""
    global driver
    try:
        if driver is None: first_init_driver()
        if len(driver.get_all_sessions()) == 0:
            return {"status": "error", "msg": "没有可用的浏览器标签页，查L3记忆分析原因。"}
        tabs = []
        for sess in driver.get_all_sessions(): 
            sess.pop('connected_at', None)
            sess.pop('type', None)
            sess['url'] = sess.get('url', '')[:50] + ("..." if len(sess.get('url', '')) > 50 else "")
            tabs.append(sess)
        if switch_tab_id: driver.default_session_id = switch_tab_id
        result = {
            "status": "success",
            "metadata": {
                "tabs_count": len(tabs), "tabs": tabs,
                "active_tab": driver.default_session_id
            }
        }
        if not tabs_only: 
            importlib.reload(simphtml); result["content"] = simphtml.get_html(driver, cutlist=True, maxchars=maxlen, text_only=text_only)
            if text_only: result['content'] = smart_format(result['content'], max_str_len=maxlen//3, omit_str='\n\n[omitted long content]\n\n')
        return result
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}
    
def format_error(e):
    exc_type, exc_value, exc_traceback = sys.exc_info()
    tb = traceback.extract_tb(exc_traceback)
    if tb:
        f = tb[-1]
        fname = os.path.basename(f.filename)
        return f"{exc_type.__name__}: {str(e)} @ {fname}:{f.lineno}, {f.name} -> `{f.line}`"
    return f"{exc_type.__name__}: {str(e)}"

def log_memory_access(path):
    if 'memory' not in path: return
    stats_file = os.path.join(script_dir, 'memory/file_access_stats.json')
    try:
        with open(stats_file, 'r', encoding='utf-8') as f: stats = json.load(f)
    except: stats = {}
    fname = os.path.basename(path)
    stats[fname] = {'count': stats.get(fname, {}).get('count', 0) + 1, 'last': datetime.now().strftime('%Y-%m-%d')}
    with open(stats_file, 'w', encoding='utf-8') as f: json.dump(stats, f, indent=2, ensure_ascii=False)

def web_execute_js(script, switch_tab_id=None, no_monitor=False):
    """执行 JS 脚本来控制浏览器，并捕获结果和页面变化"""
    global driver
    try:
        if driver is None: first_init_driver()
        if len(driver.get_all_sessions()) == 0: return {"status": "error", "msg": "没有可用的浏览器标签页，查L3记忆分析原因。"}
        if switch_tab_id: driver.default_session_id = switch_tab_id
        result = simphtml.execute_js_rich(script, driver, no_monitor=no_monitor)
        return result
    except Exception as e: return {"status": "error", "msg": format_error(e)}

def expand_file_refs(text, base_dir=None):
    """展开文本中的 {{file:路径:起始行:结束行}} 引用为实际文件内容。
    可与普通文本混排。展开失败抛 ValueError。
    base_dir: 相对路径的基准目录，默认为进程 cwd"""
    pattern = r'\{\{file:(.+?):(\d+):(\d+)\}\}'
    def replacer(match):
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        path = os.path.abspath(os.path.join(base_dir or '.', path))
        if not os.path.isfile(path): raise ValueError(f"引用文件不存在: {path}")
        with open(path, 'r', encoding='utf-8') as f: lines = f.readlines()
        if start < 1 or end > len(lines) or start > end: raise ValueError(f"行号越界: {path} 共{len(lines)}行, 请求{start}-{end}")
        return ''.join(lines[start-1:end])
    return re.sub(pattern, replacer, text)
    
def file_patch(path: str, old_content: str, new_content: str):
    """在文件中寻找唯一的 old_content 块并替换为 new_content"""
    path = str(Path(path).resolve())
    try:
        if not os.path.exists(path): return {"status": "error", "msg": "文件不存在"}
        with open(path, 'r', encoding='utf-8') as f: full_text = f.read()
        if not old_content: return {"status": "error", "msg": "old_content 为空，请确认 arguments"}
        count = full_text.count(old_content)
        if count == 0: return {"status": "error", "msg": "未找到匹配的旧文本块，建议：先用 file_read 确认当前内容，再分小段进行 patch。若多次失败则询问用户，严禁自行使用 overwrite 或代码替换。"}
        if count > 1: return {"status": "error", "msg": f"找到 {count} 处匹配，无法确定唯一位置。请提供更长、更具体的旧文本块以确保唯一性。建议：包含上下文行来增强特征，或分小段逐个修改。"}
        updated_text = full_text.replace(old_content, new_content)
        with open(path, 'w', encoding='utf-8') as f: f.write(updated_text)
        return {"status": "success", "msg": "文件局部修改成功"}
    except Exception as e: return {"status": "error", "msg": str(e)}

_read_dirs = set()
def _scan_files(base, depth=2):
    try:
        for e in os.scandir(base):
            if e.is_file(): yield (e.name, e.path)
            elif depth > 0 and e.is_dir(follow_symlinks=False): yield from _scan_files(e.path, depth - 1)
    except (PermissionError, OSError): pass
def file_read(path, start=1, keyword=None, count=200, show_linenos=True):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            stream = ((i, l.rstrip('\r\n')) for i, l in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)
            if keyword:
                before = collections.deque(maxlen=count//3)
                for i, l in stream:
                    if keyword.lower() in l.lower():
                        res = list(before) + [(i, l)] + list(itertools.islice(stream, count - len(before) - 1))
                        break
                    before.append((i, l))
                else: return f"Keyword '{keyword}' not found after line {start}. Falling back to content from line {start}:\n\n" \
                               + file_read(path, start, None, count, show_linenos)
            else: res = list(itertools.islice(stream, count))
            realcnt = len(res); L_MAX = min(max(100, 256000//max(realcnt,1)), 8000); TAG = " ... [TRUNCATED]"
            remaining = sum(1 for _ in itertools.islice(stream, 5000))
            total_lines = (res[0][0] - 1 if res else start - 1) + realcnt + remaining
            tl_str = f"{total_lines}+" if remaining >= 5000 else str(total_lines)
            partial = total_lines > realcnt
            total_tag = f"[FILE] {tl_str} lines" + (f" | PARTIAL showing {realcnt}; assess need for more" if partial else "") + "\n"
            res = [(i, l if len(l) <= L_MAX else l[:L_MAX] + TAG) for i, l in res]
            result = "\n".join(f"{i}|{l}" if show_linenos else l for i, l in res)
            if show_linenos: result = total_tag + result
            elif partial: result += f"\n\n[FILE PARTIAL: showing {realcnt}/{tl_str} lines; assess need for more]"
            _read_dirs.add(os.path.dirname(os.path.abspath(path)))
            return result
    except FileNotFoundError:
        msg = f"Error: File not found: {path}"
        try:
            tgt = os.path.basename(path); scan = os.path.dirname(os.path.dirname(os.path.abspath(path)))
            roots = [scan] + [d for d in _read_dirs if not d.startswith(scan)]
            cands = list(itertools.islice((c for base in roots for c in _scan_files(base)), 2000))
            top = sorted([(difflib.SequenceMatcher(None, tgt.lower(), c[0].lower()).ratio(), c) for c in cands[:2000]], key=lambda x: -x[0])[:5]
            top = [(s, c) for s, c in top if s > 0.3]
            if top: msg += "\n\nDid you mean:\n" + "\n".join(f"  {c[1]}  ({s:.0%})" for s, c in top)
        except Exception: pass
        return msg
    except Exception as e: return f"Error: {str(e)}"

def smart_format(data, max_str_len=100, omit_str=' ... '):
    if not isinstance(data, str): data = str(data)
    if len(data) < max_str_len + len(omit_str)*2: return data
    return f"{data[:max_str_len//2]}{omit_str}{data[-max_str_len//2:]}"

def consume_file(dr, file):
    if dr and os.path.exists(os.path.join(dr, file)): 
        with open(os.path.join(dr, file), encoding='utf-8', errors='replace') as f: content = f.read()
        os.remove(os.path.join(dr, file))
        return content

class GenericAgentHandler(BaseHandler):
    '''Generic Agent 工具库，包含多种工具的实现。工具函数自动加上了 do_ 前缀。实际工具名没有前缀。'''
    def __init__(self, parent, last_history=None, cwd='./temp'):
        self.parent = parent
        self.working = {}
        self.cwd = cwd;  self.current_turn = 0
        self.history_info = last_history if last_history else []
        self.code_stop_signal = threading.Event()
        self._done_hooks = []
        self.workflow_permission_policy = None
        self.workflow_permission_context = {}
        self.workflow_permission_event_callback = None
        self._workflow_permission_profile_selected = False
        # 主会话三档权限（read_only/ask/full_access）。默认 None = 全开（等价 full_access）。
        # workflow 子 agent 的 workflow_permission_policy 优先级更高，两者都设时以 workflow 为准。
        self.permission_mode_policy = None
        self.subagent_permission_policy = None
        self.subagent_permission_event_callback = None
        # ask 档阻塞审批 runtime（permission_request/response）；None = 无 UI 时 ask→deny。
        self.permission_runtime = None

    def dispatch(self, tool_name, args, response, index=0, tool_num=1):
        policy = getattr(self, 'workflow_permission_policy', None)
        if policy is not None:
            decision = self._check_workflow_permission(tool_name, args or {})
            if decision.action != 'allow':
                # workflow 内 ask 仍非阻塞（大件 #3）；主会话 ask 见下方 mode_policy 路径。
                status = 'approval_required' if decision.action == 'ask' else 'error'
                yield f"[Permission] {decision.action}: {tool_name} ({decision.reason})\n"
                ret = StepOutcome({"status": status, "permission": decision.to_dict()}, next_prompt="\n")
                _ = yield from try_call_generator(self.tool_after_callback, tool_name, args or {}, response, ret)
                return ret
        else:
            subagent_policy = getattr(self, 'subagent_permission_policy', None)
            if subagent_policy is not None:
                decision = subagent_policy.evaluate(tool_name, args or {})
                subagent_permission_callback = getattr(self, 'subagent_permission_event_callback', None)
                if subagent_permission_callback is not None:
                    subagent_permission_callback(tool_name, args or {}, decision)
                if decision.action != 'allow':
                    status = 'approval_required' if decision.action == 'ask' else 'error'
                    yield f"[Permission] {decision.action}: {tool_name} ({decision.reason})\n"
                    ret = StepOutcome({"status": status, "permission": decision.to_dict()}, next_prompt="\n")
                    _ = yield from try_call_generator(self.tool_after_callback, tool_name, args or {}, response, ret)
                    return ret
            mode_policy = getattr(self, 'permission_mode_policy', None)
            if mode_policy is not None:
                decision = mode_policy.evaluate(tool_name, args or {})
                if decision.action == 'deny':
                    yield f"[Permission] deny: {tool_name} ({decision.reason})\n"
                    ret = StepOutcome({"status": "error", "permission": decision.to_dict()}, next_prompt="\n")
                    _ = yield from try_call_generator(self.tool_after_callback, tool_name, args or {}, response, ret)
                    return ret
                if decision.action == 'ask':
                    # 阻塞审批：accept 后继续执行工具；deny / 无 UI / stop → 不执行。
                    from permission_runtime import ACCEPT, DENY
                    runtime = getattr(self, 'permission_runtime', None)
                    yield f"[Permission] waiting for approval: {tool_name} ({decision.reason})\n"
                    user_decision = DENY
                    if runtime is not None:
                        parent = getattr(self, 'parent', None)
                        def _stop_check():
                            return bool(getattr(parent, 'stop_sig', False))
                        user_decision = runtime.wait_for_decision(
                            tool_name, args or {}, decision.reason, stop_check=_stop_check
                        )
                    if user_decision != ACCEPT:
                        yield f"[Permission] deny: {tool_name} (user_or_headless)\n"
                        denied = {
                            "status": "error",
                            "permission": {
                                **decision.to_dict(),
                                "action": "deny",
                                "user_decision": user_decision if user_decision in (ACCEPT, DENY) else DENY,
                                "message": (
                                    f"User denied tool `{tool_name}` (or no approval UI / stopped). "
                                    "Do not retry the same write/execute blindly; adjust the plan or ask the user."
                                ),
                            },
                        }
                        ret = StepOutcome(denied, next_prompt="\n")
                        _ = yield from try_call_generator(self.tool_after_callback, tool_name, args or {}, response, ret)
                        return ret
                    yield f"[Permission] accept: {tool_name}\n"
        if str(tool_name).startswith('mcp__'):
            args = args or {}
            args['_index'] = index; args['_tool_num'] = tool_num
            _ = yield from try_call_generator(self.tool_before_callback, tool_name, args, response)
            ret = yield from self._dispatch_mcp_tool(tool_name, args, response)
            _ = yield from try_call_generator(self.tool_after_callback, tool_name, args, response, ret)
            return ret
        return (yield from super().dispatch(tool_name, args, response, index=index, tool_num=tool_num))

    def _check_workflow_permission(self, tool_name, args):
        policy = self.workflow_permission_policy
        decision = policy.evaluate(tool_name, args)
        if not self._workflow_permission_profile_selected:
            self._workflow_permission_profile_selected = True
            self._emit_workflow_permission_event('permission_profile_selected', tool_name, decision)
        event_type = 'tool_allowed' if decision.action == 'allow' else 'tool_denied'
        self._emit_workflow_permission_event(event_type, tool_name, decision)
        return decision

    def _emit_workflow_permission_event(self, event_type, tool_name, decision):
        callback = getattr(self, 'workflow_permission_event_callback', None)
        if not callback:
            return
        from workflow_permissions import build_permission_event
        event = build_permission_event(
            event_type,
            context=getattr(self, 'workflow_permission_context', None),
            tool_name=tool_name,
            decision=decision,
        )
        callback(event)

    def _dispatch_mcp_tool(self, tool_name, args, response):
        call_args = {k: v for k, v in (args or {}).items() if not str(k).startswith('_')}
        yield f"[Action] Calling MCP tool: {tool_name}\n"
        try:
            from mcp_runtime import call_mcp_tool, mcp_cancellation_scope
            with mcp_cancellation_scope(self.code_stop_signal):
                result = call_mcp_tool(tool_name, call_args)
        except Exception as e:
            result = {"status": "error", "msg": format_error(e)}
        yield f"[Status] MCP {result.get('status', 'unknown')}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def cancel(self):
        self.code_stop_signal.set()

    def _get_abs_path(self, path):
        if not path: return ""
        return os.path.abspath(os.path.join(self.cwd, path))   

    def _extract_code_block(self, response, code_type):
        code_type = {'python':'python|py', 'powershell':'powershell|ps1|pwsh', 'bash':'bash|sh|shell'}.get(code_type, re.escape(code_type))
        matches = re.findall(rf"```(?:{code_type})\n(.*?)\n```", response.content, re.DOTALL)
        return matches[-1].strip() if matches else None

    def do_code_run(self, args, response):
        '''执行代码片段，有长度限制，不允许代码中放大量数据，如有需要应当通过文件读取进行。'''
        code_type = args.get("type", "python")
        code = args.get("code") or args.get("script")
        if not code:
            code = self._extract_code_block(response, code_type)
            if not code: return StepOutcome("[Error] Code missing. Must use reply code block or 'script' arg.", next_prompt="\n")
        try: timeout = int(args.get("timeout", 60))
        except: timeout = 60
        raw_path = os.path.join(self.cwd, args.get("cwd", './'))
        cwd = os.path.normpath(os.path.abspath(raw_path))
        code_cwd = os.path.normpath(self.cwd)
        maxlen = 10000 // args.get('_tool_num', 1)
        if code_type == 'python' and args.get("inline_eval"):
            ns = {'handler':self, 'parent':self.parent, 'history':json.dumps(self.parent.llmclient.backend.history)}
            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                try:
                    try: result = repr(eval(code, ns))
                    except SyntaxError: exec(code, ns); result = ns.get('_r', 'OK')
                except Exception as e: result = f'Error: {e}'
            finally: os.chdir(old_cwd)
        else: result = yield from code_run(code, code_type, timeout, cwd, code_cwd=code_cwd, stop_signal=self.code_stop_signal, maxlen=maxlen)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_ask_user(self, args, response):
        question = args.get("question", "请提供输入：")
        candidates = args.get("candidates", [])
        result = ask_user(question, candidates)
        yield f"Waiting for your answer ...\n"
        return StepOutcome(result, next_prompt="", should_exit=True)
    
    def do_web_scan(self, args, response):
        '''获取当前页面内容和标签页列表。也可用于切换标签页。
        注意：HTML经过简化，边栏/浮动元素等可能被过滤。如需查看被过滤的内容请用execute_js。
        tabs_only=true时仅返回标签页列表，不获取HTML（省token）'''
        tabs_only = args.get("tabs_only", False)
        switch_tab_id = args.get("switch_tab_id", None)
        text_only = args.get("text_only", False)
        maxlen = 35000 // args.get('_tool_num', 1)
        result = web_scan(tabs_only=tabs_only, switch_tab_id=switch_tab_id, text_only=text_only, maxlen=maxlen)
        content = result.pop("content", None)
        yield f'[Info] {str(result)}\n'
        if content: result = json.dumps(result, ensure_ascii=False, default=json_default) + f"\n```html\n{content}\n```"
        next_prompt = "\n"
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_web_execute_js(self, args, response):
        '''web情况下的优先使用工具，执行任何js达成对浏览器的*完全*控制。支持将结果保存到文件供后续读取分析。'''
        script = args.get("script", "") or self._extract_code_block(response, "javascript")
        if not script: return StepOutcome("[Error] Script missing. Use ```javascript block or 'script' arg.", next_prompt="\n")
        abs_path = self._get_abs_path(script.strip())
        if os.path.isfile(abs_path):
            with open(abs_path, 'r', encoding='utf-8') as f: script = f.read()
        save_to_file = args.get("save_to_file", "")
        switch_tab_id = args.get("switch_tab_id") or args.get("tab_id")
        no_monitor = args.get("no_monitor", False)
        result = web_execute_js(script, switch_tab_id=switch_tab_id, no_monitor=no_monitor)
        if save_to_file and "js_return" in result:
            content = str(result["js_return"] or '')
            abs_path = self._get_abs_path(save_to_file)
            result["js_return"] = smart_format(content, max_str_len=170)
            try:
                with open(abs_path, 'w', encoding='utf-8') as f: f.write(str(content))
                result["js_return"] += f"\n\n[已保存完整内容到 {abs_path}]"
            except: result['js_return'] += f"\n\n[保存失败，无法写入文件 {abs_path}]"
        show = smart_format(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), max_str_len=300)
        try: print("Web Execute JS Result:", show)
        except: pass
        yield f"JS 执行结果:\n{show}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        result = json.dumps(result, ensure_ascii=False, default=json_default)
        maxlen = 8000 // args.get('_tool_num', 1)
        return StepOutcome(smart_format(result, max_str_len=maxlen), next_prompt=next_prompt)
    
    def do_file_patch(self, args, response):
        path = self._get_abs_path(args.get("path", ""))
        yield f"[Action] Patching file: {path}\n"
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        try: new_content = expand_file_refs(new_content, base_dir=self.cwd)
        except ValueError as e:
            yield f"[Status] ❌ 引用展开失败: {e}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        result = file_patch(path, old_content, new_content)
        yield f"\n{str(result)}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_file_write(self, args, response):
        '''用于对整个文件的大量处理，精细修改要用file_patch。
        需要将要写入的内容放在<file_content>标签内，或者放在代码块中'''
        path = self._get_abs_path(args.get("path", ""))
        mode = args.get("mode", "overwrite")  # overwrite/append/prepend
        action_str = {"prepend": "Prepending to", "append": "Appending to"}.get(mode, "Overwriting")
        yield f"[Action] {action_str} file: {os.path.basename(path)}\n"

        def extract_robust_content(text):
            tags = re.findall(r"<file_content[^>]*>(.*?)</file_content>", text, re.DOTALL)
            if tags: return tags[-1].strip()
            blocks = re.findall(r"```[^\n]*\n([\s\S]*?)```", text)
            if blocks: return blocks[-1].strip()
            return None
        
        content = args.get('content') or extract_robust_content(response.content)
        if not content:
            yield f"[Status] ❌ 失败: 未在回复中找到<file_content>代码块内容\n"
            return StepOutcome({"status": "error", "msg": "No content found. Blank is not supported. Put content inside <file_content>...</file_content> tags in your reply body before call file_write."}, next_prompt="\n")
        try:
            new_content = expand_file_refs(content, base_dir=self.cwd)
            if mode == "prepend":
                old = open(path, 'r', encoding="utf-8").read() if os.path.exists(path) else ""
                open(path, 'w', encoding="utf-8").write(new_content + old)
            else:
                with open(path, 'a' if mode == "append" else 'w', encoding="utf-8") as f: f.write(new_content)
            yield f"[Status] ✅ {mode.capitalize()} 成功 ({len(new_content)} bytes)\n"
            next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
            return StepOutcome({"status": "success", 'writed_bytes': len(new_content)}, next_prompt=next_prompt)
        except Exception as e:
            yield f"[Status] ❌ 写入异常: {str(e)}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        
    def do_file_read(self, args, response):
        '''读取文件内容。从第start行开始读取。如有keyword则返回第一个keyword(忽略大小写)周边内容'''
        path = self._get_abs_path(args.get("path", ""))
        yield f"\n[Action] Reading file: {path}\n"
        start = args.get("start", 1)
        count = args.get("count", 200)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)
        result = file_read(path, start=start, keyword=keyword,
                           count=count, show_linenos=show_linenos)
        if show_linenos and not result.startswith("Error:"): result = '由于设置了show_linenos，以下返回信息为：(行号|)内容 。\n' + result 
        if ' ... [TRUNCATED]' in result: result += '\n\n（某些行被截断，如需完整内容可改用 code_run 读取）'
        maxlen = 20000 // args.get('_tool_num', 1)
        result = smart_format(result, max_str_len=maxlen, omit_str='\n\n[omitted long content]\n\n')
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        log_memory_access(path)
        if 'memory' in path or 'sop' in path: 
            next_prompt += "\n[SYSTEM TIPS] 正在读取记忆或SOP文件，若决定按sop执行请提取sop中的关键点（特别是靠后的）update working memory."
        return StepOutcome(result, next_prompt=next_prompt)
    
    def _in_plan_mode(self): return self.working.get('in_plan_mode')
    def _exit_plan_mode(self): self.working.pop('in_plan_mode', None)
    def enter_plan_mode(self, plan_path): 
        self.working['in_plan_mode'] = plan_path; self.max_turns = 100
        print(f"[Info] Entered plan mode with plan file: {plan_path}"); return plan_path
    def _check_plan_completion(self):
        if not os.path.isfile(p:=self._in_plan_mode() or ''): return None
        try: return len(re.findall(r'\[ \]', open(p, encoding='utf-8', errors='replace').read()))
        except: return None
    
    def do_update_working_checkpoint(self, args, response):
        '''为整个任务设定后续需要临时记忆的重点。'''
        key_info = args.get("key_info", "")
        related_sop = args.get("related_sop", "")
        if "key_info" in args: self.working['key_info'] = key_info
        if "related_sop" in args: self.working['related_sop'] = related_sop
        self.working['passed_sessions'] = 0
        yield f"[Info] Updated key_info and related_sop.\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        #next_prompt += '\n[SYSTEM TIPS] 此函数一般在任务开始或中间时调用，如果任务已成功完成应该是start_long_term_update用于结算长期记忆。\n'
        return StepOutcome({"result": "working key_info updated"}, next_prompt=next_prompt)

    def do_load_skill(self, args, response):
        '''按名称加载 Claude/Codex 风格的 SKILL.md，将完整技能说明注入下一轮上下文。'''
        skill_name = args.get("skill") or args.get("name") or ""
        skill_args = args.get("args", "")
        search_roots = args.get("search_roots")
        try:
            from skills_runtime import load_skill_content
            result = load_skill_content(skill_name, search_roots=search_roots, args=skill_args)
        except KeyError as e:
            yield f"[Warn] {e}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        except Exception as e:
            yield f"[Warn] Failed to load skill: {e}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        self.working['active_skill'] = result['name']
        if result.get('allowed_tools'):
            self.working['active_skill_allowed_tools'] = ', '.join(result['allowed_tools'])
        else:
            self.working.pop('active_skill_allowed_tools', None)
        yield f"[Info] Loaded skill {result['name']} from {result.get('source', 'local')}: {result.get('path', '')}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        next_prompt += "\n[SYSTEM TIPS] 已加载 skill。必须按上方 SKILL.md 内容执行；如技能引用相对路径，使用返回的 base_dir。"
        return StepOutcome(result, next_prompt=next_prompt)

    def _get_subagent_manager(self):
        manager = getattr(self, "subagent_manager", None)
        if manager is not None:
            return manager
        from subagent_manager import SubagentManager
        return SubagentManager(root_dir=script_dir, python_executable=sys.executable)

    def _current_backend_history_snapshot(self):
        backend = getattr(getattr(self.parent, "llmclient", None), "backend", None)
        history = getattr(backend, "history", None)
        if history is None:
            return []
        try:
            return json.loads(json.dumps(history, ensure_ascii=False, default=json_default))
        except Exception:
            return []

    def _subagent_state_payload(self, state, include_output=False, max_output_chars=8000):
        payload = {
            "task_name": state.task_name,
            "agent_path": state.agent_path,
            "pid": state.pid,
            "task_dir": state.task_dir,
            "turn_status": state.turn_status,
            "process_status": state.process_status,
            "round": state.round,
            "output_path": state.output_path,
            "final_output_path": state.final_output_path,
            "run_id": getattr(state, "run_id", None),
            "parent_session_id": getattr(state, "parent_session_id", None),
            "artifact_dir": getattr(state, "artifact_dir", None),
            "permission_profile": getattr(state, "permission_profile", None),
            "parent_permission_mode": getattr(state, "parent_permission_mode", None),
            "permission_options": getattr(state, "permission_options", None) or {},
            "agent_type": getattr(state, "agent_type", None),
            "role_source_path": getattr(state, "role_source_path", None),
            "background": getattr(state, "background", True),
            "handoff_mode": getattr(state, "handoff_mode", None),
            "handoff_reason": getattr(state, "handoff_reason", None),
            "attach_status": getattr(state, "attach_status", None),
            "ipc_mode": getattr(state, "ipc_mode", None),
            "effective_ipc_mode": getattr(state, "effective_ipc_mode", None),
            "ipc_fallback_reason": getattr(state, "ipc_fallback_reason", None),
            "ipc_endpoint": getattr(state, "ipc_endpoint", None),
            "isolation": getattr(state, "isolation", None),
            "worktree_path": getattr(state, "worktree_path", None),
            "worktree_summary": getattr(state, "worktree_summary", None),
            "worktree_cleanup": getattr(state, "worktree_cleanup", None),
            "updated_at": state.updated_at,
            "last_message": state.last_message,
            "last_error": state.last_error,
        }
        if include_output and state.final_output_path and os.path.exists(state.final_output_path):
            try:
                with open(state.final_output_path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                text = text.replace("\n\n[ROUND END]\n", "").rstrip()
                payload["final_output"] = smart_format(text, max_str_len=max_output_chars, omit_str="\n\n[omitted long subagent output]\n\n")
            except Exception as e:
                payload["final_output_error"] = str(e)
        return payload

    def do_spawn_agent(self, args, response):
        '''启动一个后台子智能体。默认继承当前会话上下文，子智能体拥有完整 GA 工具能力。'''
        task_name = args.get("task_name") or args.get("name")
        agent_type = args.get("agent_type") or args.get("agentType")
        role = None
        role_source_path = None
        if agent_type:
            try:
                from subagent_roles import SubagentRoleRegistry, build_role_task_message
                manager_for_roles = self._get_subagent_manager()
                role = SubagentRoleRegistry(manager_for_roles.root_dir).get(agent_type)
                role_source_path = role.source_path
                task_name = task_name or role.name
            except Exception as e:
                return StepOutcome({"status": "error", "msg": f"agent_type {agent_type} not found: {format_error(e)}"}, next_prompt="\n")
        message = args.get("message") or args.get("prompt") or ""
        if not task_name:
            return StepOutcome({"status": "error", "msg": "task_name is required"}, next_prompt="\n")
        if not message:
            return StepOutcome({"status": "error", "msg": "message is required"}, next_prompt="\n")
        if role is not None:
            message = build_role_task_message(role, message)
        raw_fork_turns = args.get("fork_turns")
        if raw_fork_turns is None and role is not None and role.fork_turns_default:
            raw_fork_turns = role.fork_turns_default
        fork_turns = str(raw_fork_turns if raw_fork_turns is not None else "all")
        fork_history = None if fork_turns.lower() == "none" else self._current_backend_history_snapshot()
        try:
            llm_no = int(args.get("llm_no", getattr(self.parent, "llm_no", 0)))
        except Exception:
            llm_no = getattr(self.parent, "llm_no", 0)
        verbose = bool(args.get("verbose", getattr(self.parent, "verbose", False)))
        permission_profile = args.get("permission_profile") or args.get("permissionProfile") or (role.permission_profile if role is not None else None) or "inherit-current-permissions"
        parent_permission_mode = args.get("parent_permission_mode") or args.get("parentPermissionMode") or getattr(self.parent, "permission_mode", None)
        role_permission_options = dict(role.permission_options or {}) if role is not None else {}
        permission_options = {
            key: args.get(key)
            for key in [
                "allowed_tools",
                "denied_tools",
                "allowed_mcp_servers",
                "denied_mcp_servers",
                "allowed_mcp_tools",
                "denied_mcp_tools",
            ]
            if args.get(key) is not None
        }
        permission_options = {**role_permission_options, **permission_options}
        manager = self._get_subagent_manager()
        yield f"[Action] Spawning subagent: {task_name}\n"
        try:
            handle = manager.spawn_agent(
                task_name,
                message,
                llm_no=llm_no,
                verbose=verbose,
                parent_session_id=getattr(self.parent, "session_id", None),
                fork_turns=fork_turns,
                fork_history=fork_history,
                permission_profile=permission_profile,
                parent_permission_mode=parent_permission_mode,
                permission_options=permission_options,
                agent_type=agent_type,
                role_source_path=role_source_path,
                background=bool(args.get("background", True)),
                ipc_mode=args.get("ipc_mode") or args.get("ipcMode") or "file",
                isolation=args.get("isolation"),
            )
            result = {
                "status": "started",
                "task_name": handle.task_name,
                "agent_path": handle.agent_path,
                "pid": handle.pid,
                "task_dir": handle.task_dir,
                "state_path": handle.state_path,
                "run_id": handle.run_id,
                "artifact_dir": handle.artifact_dir,
                "permission_profile": handle.permission_profile,
                "parent_permission_mode": handle.parent_permission_mode,
                "permission_options": handle.permission_options or {},
                "agent_type": handle.agent_type,
                "role_source_path": handle.role_source_path,
                "background": handle.background,
                "ipc_mode": handle.ipc_mode,
                "effective_ipc_mode": handle.effective_ipc_mode,
                "ipc_fallback_reason": handle.ipc_fallback_reason,
                "ipc_endpoint": handle.ipc_endpoint,
                "isolation": handle.isolation,
                "worktree_path": handle.worktree_path,
                "fork_turns": fork_turns,
            }
            yield f"[Status] Subagent {handle.agent_path} started (pid={handle.pid}).\n"
        except Exception as e:
            result = {"status": "error", "msg": format_error(e)}
            yield f"[Status] Failed to spawn subagent: {result['msg']}\n"
        return StepOutcome(result, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_list_agents(self, args, response):
        '''列出当前 root 下的子智能体状态。'''
        manager = self._get_subagent_manager()
        include_closed = bool(args.get("include_closed", False))
        include_output = bool(args.get("include_output", False))
        path_prefix = args.get("path_prefix")
        max_output_chars = max(1000, 12000 // max(1, int(args.get('_tool_num', 1))))
        states = manager.list_agents(path_prefix=path_prefix, include_closed=include_closed)
        result = {
            "status": "success",
            "agents": [
                self._subagent_state_payload(state, include_output=include_output, max_output_chars=max_output_chars)
                for state in states
            ],
        }
        yield f"[Status] Found {len(states)} subagent(s).\n"
        return StepOutcome(result, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_send_message(self, args, response):
        '''向子智能体邮箱发送消息，但不触发新一轮。'''
        target = args.get("target") or args.get("task_name")
        message = args.get("message") or ""
        if not target or not message:
            return StepOutcome({"status": "error", "msg": "target and message are required"}, next_prompt="\n")
        manager = self._get_subagent_manager()
        row = manager.send_message(target, message, author="/root")
        yield f"[Status] Message queued for {row.get('recipient')}.\n"
        return StepOutcome({"status": "queued", "message": row}, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_followup_task(self, args, response):
        '''给已完成或等待中的子智能体发送后续任务，并触发下一轮。'''
        target = args.get("target") or args.get("task_name")
        message = args.get("message") or ""
        if not target or not message:
            return StepOutcome({"status": "error", "msg": "target and message are required"}, next_prompt="\n")
        manager = self._get_subagent_manager()
        row = manager.followup_task(target, message, author="/root")
        yield f"[Status] Follow-up task queued for {row.get('recipient')}.\n"
        return StepOutcome({"status": "queued", "message": row}, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_wait_agent(self, args, response):
        '''等待子智能体 mailbox/status 更新；不读取最终输出正文。'''
        raw_targets = args.get("targets")
        target = args.get("target") or args.get("task_name")
        if raw_targets is None:
            targets = [target] if target else None
        elif isinstance(raw_targets, str):
            raw_targets = raw_targets.strip()
            targets = [raw_targets] if raw_targets else ([target] if target else None)
        else:
            targets = list(raw_targets)
            if not targets:
                targets = [target] if target else None
        try:
            timeout_s = float(args.get("timeout_seconds", args.get("timeout_s", 30)))
        except Exception:
            timeout_s = 30.0
        timeout_s = max(0.0, min(timeout_s, 3600.0))
        try:
            poll_interval_s = float(args.get("poll_interval_seconds", 0.5))
        except Exception:
            poll_interval_s = 0.5
        poll_interval_s = max(0.05, min(poll_interval_s, 10.0))
        raw_since_seq = args.get("since_event_seq") or args.get("sinceEventSeq")
        since_event_seq = None
        if raw_since_seq is not None:
            try:
                since_event_seq = int(raw_since_seq)
            except Exception:
                since_event_seq = 0
        manager = self._get_subagent_manager()
        yield f"[Action] Waiting for subagent update ({timeout_s:g}s).\n"
        result = manager.wait_agents(
            targets=targets,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            since_event_seq=since_event_seq,
        )
        data = {
            "status": "timeout" if result.timed_out else "changed",
            "message": result.message,
            "events": result.events or [],
            "next_event_seq": result.next_event_seq,
            "agents": [
                self._subagent_state_payload(state, include_output=False)
                for state in result.changed_agents
            ],
        }
        if any(agent.get("turn_status") == "completed" for agent in data["agents"]):
            data["result_hint"] = "Call read_agent_result for a completed subagent when you need its final output."
        yield f"[Status] {data['status']}: {len(data['agents'])} update(s).\n"
        return StepOutcome(data, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_read_agent_result(self, args, response):
        '''读取一个已完成子智能体的最终输出；和 wait_agent 的事件等待职责分离。'''
        target = args.get("target") or args.get("task_name")
        if not target:
            return StepOutcome({"status": "error", "msg": "target is required"}, next_prompt="\n")
        artifact_id = args.get("artifact_id") or args.get("artifactId")
        include_transcript_replay = bool(args.get("include_transcript_replay") or args.get("includeTranscriptReplay"))
        include_transcript_timeline = bool(args.get("include_transcript_timeline") or args.get("includeTranscriptTimeline"))
        include_resume_context = bool(args.get("include_resume_context") or args.get("includeResumeContext"))
        resume_context_edits = args.get("resume_context_edits") or args.get("resumeContextEdits") or None
        try:
            max_output_chars = int(args.get("max_output_chars", max(1000, 12000 // max(1, int(args.get('_tool_num', 1))))))
        except Exception:
            max_output_chars = 8000
        max_output_chars = max(1000, min(max_output_chars, 50000))
        manager = self._get_subagent_manager()
        try:
            state = manager.read_agent(target)
            payload = self._subagent_state_payload(state, include_output=not bool(artifact_id), max_output_chars=max_output_chars)
            if artifact_id:
                from subagent_artifacts import SubagentArtifactStore

                artifact = SubagentArtifactStore(state.artifact_dir).get(artifact_id)
                artifact_path = artifact.get("path")
                with open(artifact_path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                text = text.replace("\n\n[ROUND END]\n", "").rstrip()
                payload.update(
                    {
                        "artifact_id": artifact.get("artifact_id"),
                        "artifact": artifact,
                        "final_output_path": artifact_path,
                        "final_output": smart_format(
                            text,
                            max_str_len=max_output_chars,
                            omit_str="\n\n[omitted long subagent output]\n\n",
                        ),
                    }
                )
                status = "success"
            elif state.turn_status != "completed":
                status = "not_completed"
            elif payload.get("final_output") is None:
                status = "missing_result"
            else:
                status = "success"
            if include_transcript_replay or include_transcript_timeline or include_resume_context:
                parent_session_id = getattr(state, "parent_session_id", None)
                run_id = getattr(state, "run_id", None)
                if parent_session_id and run_id:
                    from subagent_transcript import SubagentTranscriptStore

                    transcript_store = SubagentTranscriptStore(manager.temp_dir / "sessions")
                    if include_transcript_replay:
                        payload["transcript_replay"] = transcript_store.replay(parent_session_id, run_id)
                    if include_transcript_timeline:
                        payload["transcript_timeline"] = transcript_store.build_replay_timeline(parent_session_id, run_id)
                    if include_resume_context:
                        payload["resume_context"] = transcript_store.build_resume_context(parent_session_id, run_id, edits=resume_context_edits)
                else:
                    missing = {"status": "missing", "reason": "parent_session_id_or_run_id_missing"}
                    if include_transcript_replay:
                        payload["transcript_replay"] = missing
                    if include_transcript_timeline:
                        payload["transcript_timeline"] = missing
                    if include_resume_context:
                        payload["resume_context"] = missing
            data = {"status": status, "agent": payload}
            yield f"[Status] Read result for {state.agent_path}: {status}.\n"
        except Exception as e:
            data = {"status": "error", "msg": format_error(e)}
            yield f"[Status] Failed to read subagent result: {data['msg']}\n"
        return StepOutcome(data, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_resume_agent(self, args, response):
        '''基于 sidechain transcript 恢复一个已关闭子智能体并启动下一轮。'''
        target = args.get("target") or args.get("task_name")
        message = args.get("message") or args.get("prompt") or ""
        if not target or not message:
            return StepOutcome({"status": "error", "msg": "target and message are required"}, next_prompt="\n")
        manager = self._get_subagent_manager()
        try:
            result = manager.resume_agent(target, message, author="/root")
            data = {
                "status": "resumed",
                "target": result.target,
                "previous_state": self._subagent_state_payload(result.previous_state),
                "handle": {
                    "task_name": result.handle.task_name,
                    "agent_path": result.handle.agent_path,
                    "pid": result.handle.pid,
                    "task_dir": result.handle.task_dir,
                    "state_path": result.handle.state_path,
                    "run_id": result.handle.run_id,
                    "artifact_dir": result.handle.artifact_dir,
                    "permission_profile": result.handle.permission_profile,
                    "parent_permission_mode": result.handle.parent_permission_mode,
                    "permission_options": result.handle.permission_options or {},
                    "agent_type": result.handle.agent_type,
                    "background": result.handle.background,
                    "ipc_mode": result.handle.ipc_mode,
                    "effective_ipc_mode": result.handle.effective_ipc_mode,
                    "ipc_fallback_reason": result.handle.ipc_fallback_reason,
                    "isolation": result.handle.isolation,
                    "worktree_path": result.handle.worktree_path,
                    "handoff_mode": result.handle.handoff_mode,
                    "handoff_reason": result.handle.handoff_reason,
                },
                "resume_context": result.resume_context,
            }
            yield f"[Status] Resumed subagent {result.target}.\n"
        except Exception as e:
            data = {"status": "error", "msg": format_error(e)}
            yield f"[Status] Failed to resume subagent: {data['msg']}\n"
        return StepOutcome(data, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_foreground_agent(self, args, response):
        '''请求子智能体切到前台观察状态；当前版本只记录 handoff 状态，不接管交互式 TUI。'''
        return self._do_handoff_agent(args, handoff_mode="foreground")

    def do_background_agent(self, args, response):
        '''请求子智能体切回后台队列状态；当前版本只记录 handoff 状态。'''
        return self._do_handoff_agent(args, handoff_mode="background")

    def _do_handoff_agent(self, args, *, handoff_mode):
        target = args.get("target") or args.get("task_name")
        if not target:
            return StepOutcome({"status": "error", "msg": "target is required"}, next_prompt="\n")
        reason = args.get("reason") or f"parent_{handoff_mode}"
        manager = self._get_subagent_manager()
        try:
            if handoff_mode == "foreground":
                result = manager.request_foreground(target, reason=reason)
            else:
                result = manager.request_background(target, reason=reason)
            data = {
                "status": "handoff_requested",
                "target": result.target,
                "handoff_mode": result.handoff_mode,
                "reason": result.reason,
                "previous_state": self._subagent_state_payload(result.previous_state),
                "updated_state": self._subagent_state_payload(result.updated_state),
            }
            yield f"[Status] Requested {handoff_mode} handoff for {result.target}.\n"
        except Exception as e:
            data = {"status": "error", "msg": format_error(e)}
            yield f"[Status] Failed to request {handoff_mode} handoff: {data['msg']}\n"
        return StepOutcome(data, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_attach_agent(self, args, response):
        '''附着到子智能体的实时输出流：切到前台 handoff 状态并按 since_offset 增量读取输出。'''
        return self._do_attach_stream(args, attach_status="attached")

    def do_detach_agent(self, args, response):
        '''从子智能体输出流分离：读取剩余增量输出并切回后台 handoff 状态。'''
        return self._do_attach_stream(args, attach_status="detached")

    def _do_attach_stream(self, args, *, attach_status):
        target = args.get("target") or args.get("task_name")
        if not target:
            return StepOutcome({"status": "error", "msg": "target is required"}, next_prompt="\n")
        reason = args.get("reason") or ("parent_attach" if attach_status == "attached" else "parent_detach")
        since_offset = args.get("since_offset") or 0
        max_chars = args.get("max_chars")
        manager = self._get_subagent_manager()
        try:
            if attach_status == "attached":
                result = manager.attach_agent(target, since_offset=since_offset, max_chars=max_chars, reason=reason)
            else:
                result = manager.detach_agent(target, since_offset=since_offset, max_chars=max_chars, reason=reason)
            data = {
                "status": result.attach_status,
                "target": result.target,
                "handoff_mode": result.handoff_mode,
                "reason": result.reason,
                "output_path": result.output_path,
                "stream_text": result.stream_text,
                "stream_offset": result.stream_offset,
                "next_stream_offset": result.next_stream_offset,
                "stream_truncated": result.stream_truncated,
                "stream_eof": result.stream_eof,
                "next_event_seq": result.next_event_seq,
                "state": self._subagent_state_payload(result.state),
            }
            yield f"[Status] {result.attach_status.capitalize()} subagent {result.target} stream.\n"
        except Exception as e:
            data = {"status": "error", "msg": format_error(e)}
            yield f"[Status] Failed to {attach_status[:-2]} subagent stream: {data['msg']}\n"
        return StepOutcome(data, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_interrupt_agent(self, args, response):
        '''请求子智能体中断当前轮次；子进程会尽量保留以便后续 followup_task。'''
        target = args.get("target") or args.get("task_name")
        if not target:
            return StepOutcome({"status": "error", "msg": "target is required"}, next_prompt="\n")
        reason = args.get("reason") or "parent_interrupt"
        manager = self._get_subagent_manager()
        try:
            result = manager.interrupt_agent(target, reason=reason)
            data = {
                "status": "interrupt_requested",
                "target": result.target,
                "stop_path": result.stop_path,
                "previous_state": self._subagent_state_payload(result.previous_state),
            }
            yield f"[Status] Interrupt requested for {result.previous_state.agent_path}.\n"
        except Exception as e:
            data = {"status": "error", "msg": format_error(e)}
            yield f"[Status] Failed to interrupt subagent: {data['msg']}\n"
        return StepOutcome(data, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def do_close_agent(self, args, response):
        '''关闭子智能体并返回关闭前状态；root agent 不能关闭。'''
        target = args.get("target") or args.get("task_name")
        if not target:
            return StepOutcome({"status": "error", "msg": "target is required"}, next_prompt="\n")
        reason = args.get("reason") or "parent_cleanup"
        try:
            grace_s = float(args.get("grace_seconds", args.get("grace_s", 2)))
        except Exception:
            grace_s = 2.0
        grace_s = max(0.0, min(grace_s, 60.0))
        cleanup_worktree = bool(args.get("cleanup_worktree") or args.get("cleanupWorktree"))
        manager = self._get_subagent_manager()
        try:
            result = manager.close_agent(target, reason=reason, grace_s=grace_s, cleanup_worktree=cleanup_worktree)
            data = {
                "status": "closed",
                "target": result.previous_state.agent_path,
                "previous_status": self._subagent_state_payload(result.previous_state),
                "closed_state": self._subagent_state_payload(result.closed_state),
                "final_output_path": result.final_output_path,
            }
            yield f"[Status] Closed subagent {result.previous_state.agent_path}.\n"
        except Exception as e:
            data = {"status": "error", "msg": format_error(e)}
            yield f"[Status] Failed to close subagent: {data['msg']}\n"
        return StepOutcome(data, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def _retry_or_exit(self, prompt):
        self._empty_ct = getattr(self, '_empty_ct', 0) + 1
        if self._empty_ct >= 3: return StepOutcome({}, should_exit=True)
        return StepOutcome({}, next_prompt=prompt)

    def do_no_tool(self, args, response):
        '''这是一个特殊工具，由引擎自主调用，不要包含在TOOLS_SCHEMA里。
        当模型在一轮中未显式调用任何工具时，由引擎自动触发。
        二次确认仅在回复几乎只包含<thinking>/<summary>和一段大代码块时触发。'''
        content = getattr(response, 'content', '') or ""
        thinking = getattr(response, 'thinking', '') or ""
        if not response or (not content.strip() and not thinking.strip()):
            yield "[Warn] LLM returned an empty response. Retrying...\n"
            return self._retry_or_exit("[System] Blank response, regenerate and tooluse")
        if '[!!! 流异常中断' in content[-100:] or '!!!Error:' in content[-100:]:
            return self._retry_or_exit("[System] Incomplete response. Regenerate and tooluse.")
        if 'max_tokens !!!]' in content[-100:]:
            return self._retry_or_exit("[System] max_tokens limit reached. Use multi small steps to do it.")
        
        if self._in_plan_mode() and any(kw in content for kw in ['任务完成', '全部完成', '已完成所有', '🏁']):
            if 'VERDICT' not in content and '[VERIFY]' not in content and '验证subagent' not in content:
                yield "[Warn] Plan模式完成声明拦截。\n"
                return StepOutcome({}, next_prompt="⛔ [验证拦截] 检测到你在plan模式下声称完成，但未执行[VERIFY]验证步骤。请先按plan_sop §四启动验证subagent，获得VERDICT后才能声称完成。")
            
        # 2. 检测"包含较大代码块但未调用工具"的情况
        # 关键特征：恰好1个大代码块 + 代码块直接结尾（后面只有空白）
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{50,}?```"
        blocks = re.findall(code_block_pattern, content)
        if len(blocks) == 1:
            m = re.search(code_block_pattern, content)
            after_block = content[m.end():]
            if not after_block.strip():
                residual = content.replace(m.group(0), "")
                residual = re.sub(r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE)
                residual = re.sub(r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE)
                clean_residual = re.sub(r"\s+", "", residual)
                if len(clean_residual) <= 30:
                    yield "[Info] Detected large code block without tool call and no extra natural language. Requesting clarification.\n"
                    next_prompt = (
                        "[System] 检测到你在上一轮回复中主要内容是较大代码块，且本轮未调用任何工具。\n"
                        "如果这些代码需要执行、写入文件或进一步分析，请重新组织回复并显式调用相应工具"
                        "（例如：code_run、file_write、file_patch 等）；\n"
                        "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                        "并明确是否还需要额外的实际操作。"
                    )
                    return StepOutcome({}, next_prompt=next_prompt)
                
        if self._in_plan_mode():
            remaining = self._check_plan_completion()
            if remaining == 0:
                self._exit_plan_mode(); yield "[Info] Plan完成：plan.md中0个[ ]残留，退出plan模式。\n"
        
        yield "[Info] Final response to user.\n"
        return StepOutcome(response, next_prompt=None)
    
    def do_start_long_term_update(self, args, response):
        '''Agent觉得当前任务完成后有重要信息需要记忆时调用此工具。'''
        prompt = '''### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。
本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。
**如果没有经验证的，未来能用上的信息，忽略本次调用！**
**只能提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1
- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP（只记你被坑得多次重试的核心要点）
**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节、只是做了但没有验证的信息
**操作**：严格遵循提供的L0的记忆更新SOP。先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，保证对记忆库最小局部修改。\n
''' + get_global_memory()
        yield "[Info] Start distilling good memory for long-term storage.\n"
        path = './memory/memory_management_sop.md'
        if os.path.exists(path): result = 'This is L0:\n' + file_read(path, show_linenos=False)
        else: result = "Memory Management SOP not found. Do not update memory."
        return StepOutcome(result, next_prompt=prompt)

    def do_codex_lesson_update(self, args, response):
        '''记录从Codex蒸馏packet中由LLM提议的候选经验；工具负责校验、去重和候选落盘。'''
        try:
            from memory.codex_session_distill import DistillState, codex_lesson_update
        except Exception:
            import codex_session_distill
            DistillState = codex_session_distill.DistillState
            codex_lesson_update = codex_session_distill.codex_lesson_update
        state_dir = args.get("state_dir") or os.path.join(script_dir, "memory", "codex_distill")
        evidence = args.get("evidence") or args.get("evidence_signals") or []
        if isinstance(evidence, str): evidence = [evidence]
        result = codex_lesson_update(
            DistillState(state_dir),
            title=args.get("title", ""),
            guidance=args.get("guidance", ""),
            category=args.get("category", "workflow"),
            evidence=evidence,
            source_hash=args.get("source_hash", ""),
            confidence=args.get("confidence", 0.5),
        )
        if result.get("status") == "candidate_recorded":
            yield f"[Info] Codex lesson candidate recorded: {result['candidate'].get('id')}\n"
        else:
            yield f"[Warn] Codex lesson candidate rejected: {result.get('reason')}\n"
        return StepOutcome(result, next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0))

    def _fold_earlier(self, lines):
        FALLBACK = '直接回答了用户问题'
        parts, cnt, last = [], 0, ''
        def flush():
            if cnt:
                if FALLBACK in last: parts.append(f'[Agent]（{cnt} turns）')
                else: parts.append(f'{last}（{cnt} turns）')
        for line in lines:
            if line.startswith('[USER]'):
                flush(); parts.append(line); cnt = 0; last = ''
            else: cnt += 1; last = line
        flush()
        return "\n".join(parts[-150:])

    def _get_anchor_prompt(self, skip=False):
        if skip: return "\n"
        h = self.history_info; W = 30
        earlier = f'<earlier_context>\n{self._fold_earlier(h[:-W])}\n</earlier_context>\n' if len(h) > W else ""
        h_str = "\n".join(h[-W:])
        prompt = f"\n### [WORKING MEMORY]\n{earlier}<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"
        if self.working.get('key_info'): prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
        if self.working.get('related_sop'): prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"
        if self.working.get('active_skill'):
            prompt += f"\n<active_skill>{self.working.get('active_skill')}</active_skill>"
            if self.working.get('active_skill_allowed_tools'):
                prompt += f"\n<active_skill_allowed_tools>{self.working.get('active_skill_allowed_tools')}</active_skill_allowed_tools>"
        if getattr(self.parent, 'verbose', False):
            try: print(prompt)
            except: pass
        return prompt
    
    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        _c = re.sub(r'```.*?```|<thinking>.*?</thinking>', '', response.content, flags=re.DOTALL)
        rsumm = re.search(r"<summary>(.*?)</summary>", _c, re.DOTALL)
        if rsumm: summary = rsumm.group(1).strip()
        else:
            tc = tool_calls[0]; tool_name, args = tc['tool_name'], tc['args']   # at least one because no_tool
            clean_args = {k: v for k, v in args.items() if not k.startswith('_')}
            summary = f"调用工具{tool_name}, args: {clean_args}"
            if tool_name == 'no_tool': summary = "直接回答了用户问题"
            next_prompt += "\n\n\n[SYSTEM] 必须在回复文本中包含<summary>！\n\n"
        summary = smart_format(summary.replace('\n', ''), max_str_len=80)
        self.history_info.append(f'[Agent] {summary}')
        _plan = self._in_plan_mode()

        if turn % 65 == 0 and (not _plan):
            next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。必须总结情况进行ask_user，不允许继续重试。"
        elif turn % 7 == 0:
            next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。若无有效进展，必须切换策略：1. 探测物理边界 2. 请求用户协助。如有需要，可调用 update_working_checkpoint 保存关键上下文。"
        elif turn % 10 == 0: next_prompt += get_global_memory()

        if _plan and turn >= 10 and turn % 5 == 0:
            next_prompt = f"[Plan Hint] 正在计划模式。必须 file_read({_plan}) 确认当前步骤，回复开头引用：📌 当前步骤：...\n\n" + next_prompt
        if _plan and turn >= 90: next_prompt += f"\n\n[DANGER] Plan模式已运行 {turn} 轮，已达上限。必须 ask_user 汇报进度并确认是否继续。"

        injkeyinfo = consume_file(self.parent.task_dir, '_keyinfo')
        injprompt = consume_file(self.parent.task_dir, '_intervene')
        if injkeyinfo: self.working['key_info'] = self.working.get('key_info', '') + f"\n[MASTER] {injkeyinfo}"
        if injprompt: next_prompt += f"\n\n[MASTER] {injprompt}\n"
        for hook in getattr(self.parent, '_turn_end_hooks', {}).values(): hook(locals())  # current readonly
        return next_prompt

def get_global_memory():
    prompt = "\n"
    try:
        suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
        with open(os.path.join(script_dir, 'memory/global_mem_insight.txt'), 'r', encoding='utf-8', errors='replace') as f: insight = f.read()
        with open(os.path.join(script_dir, f'assets/insight_fixed_structure{suffix}.txt'), 'r', encoding='utf-8') as f: structure = f.read()
        prompt += f'cwd = {os.path.join(script_dir, "temp")} (./; tool scratch/output dir)\n'
        prompt += f'workspace root = {script_dir} (..; project files such as README.md usually live here)\n'
        prompt += f"\n[Memory] (../memory)\n"
        prompt += structure + '\n../memory/global_mem_insight.txt:\n'
        prompt += insight + "\n"
    except FileNotFoundError: pass
    return prompt
