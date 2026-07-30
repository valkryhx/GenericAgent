import os, sys, threading, queue, time, json, re, random, locale, copy
os.environ.setdefault('GA_LANG', 'zh' if any(k in (locale.getlocale()[0] or '').lower() for k in ('zh', 'chinese')) else 'en')
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

from llmcore import NativeClaudeSession, NativeOAISession
from llm_client import load_clients_from_yaml
from agent_loop import agent_runner_loop
from ga import GenericAgentHandler, smart_format, get_global_memory, format_error, consume_file
from skills_runtime import build_skill_prompt
import session_transcript
import image_codec
try:
    from compact_context import compact_agent_context, replace_log_with_compact_history
except Exception:  # 压缩核心导入失败时降级：/compact 报不可用，不影响主流程
    compact_agent_context = None
    replace_log_with_compact_history = None
from ga_agents_runtime import build_ga_project_instructions
from subagent_state import append_jsonl_event, append_parent_inbox_event, atomic_write_json, now_iso, read_json_or_none, sha256_file
from subagent_prompts import build_agent_role_usage_hint

script_dir = os.path.dirname(os.path.abspath(__file__))
# llm.yaml 热重载：记录上次成功加载的路径与 mtime，未变则跳过重建。
_llm_yaml_path: str | None = None
_llm_yaml_mtime_ns: int | None = None
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
STREAM_FLUSH_CHARS = 16


def _extract_image_paths(text):
    paths = []
    def add_path(p):
        path = p if os.path.isabs(p) else os.path.join(script_dir, p)
        if os.path.isfile(path) and path not in paths:
            paths.append(path)

    for raw in re.findall(r'"([^"]+\.(?:png|jpe?g|webp|gif|bmp))"|\'([^\']+\.(?:png|jpe?g|webp|gif|bmp))\'', text or '', re.I):
        p = next((x for x in raw if x), '')
        if p: add_path(p)

    for m in re.finditer(r'(?:[A-Za-z]:[\\/]|/)[^\r\n"\']+?\.(?:png|jpe?g|webp|gif|bmp)', text or '', re.I):
        add_path(m.group(0).strip())

    for m in re.finditer(r'\S+\.(?:png|jpe?g|webp|gif|bmp)', text or '', re.I):
        add_path(m.group(0).strip())
    return paths


def _image_block(path):
    """兼容旧调用：走有界 codec。"""
    enc = image_codec.encode_image_for_prompt(path)
    return image_codec.build_image_block(enc)


def _build_user_content_with_images(text, images=None):
    extra = _extract_image_paths(text)
    return image_codec.build_user_content_with_images(text, images=images, extra_paths=extra)


def supports_image_input(llmclient):
    """当前 backend 是否支持把本地图作为原生多模态 user content 发送。"""
    backend = getattr(llmclient, 'backend', None)
    backend = getattr(backend, 'primary', backend)
    if backend is None:
        return False
    if bool(getattr(backend, 'native_image_input', False)):
        return True
    if bool(getattr(backend, 'supports_image_input', False)):
        return True
    # Native Claude Messages 原生支持 image block；OAI 仍要求显式 flag/capability
    if isinstance(backend, NativeClaudeSession) and not isinstance(backend, NativeOAISession):
        # 允许 cfg 显式关掉
        if 'native_image_input' in getattr(backend, '__dict__', {}):
            return bool(getattr(backend, 'native_image_input'))
        return True
    if isinstance(backend, NativeOAISession):
        return bool(getattr(backend, 'native_image_input', False))
    return False


def _native_image_input_enabled(llmclient):
    """兼容旧名；语义升级为 supports_image_input。"""
    return supports_image_input(llmclient)


def _is_image_content_block(block) -> bool:
    """Claude image / OAI image_url / Responses input_image 均算原生图。"""
    if not isinstance(block, dict):
        return False
    t = block.get("type")
    if t in ("image", "input_image"):
        return True
    if t == "image_url":
        return True
    # 少数适配器把图塞在 source.type=base64
    src = block.get("source")
    if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
        return True
    return False


def count_image_blocks_in_content(content) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if _is_image_content_block(b))


def content_has_native_images(content) -> bool:
    return count_image_blocks_in_content(content) > 0


def history_has_native_images(history, *, max_messages: int = 24) -> bool:
    """扫描近期 history 是否已有原生 image block（用于「重试」等无新附件轮次）。"""
    if not isinstance(history, list) or not history:
        return False
    for msg in history[-max_messages:]:
        if not isinstance(msg, dict):
            continue
        if content_has_native_images(msg.get("content")):
            return True
    return False


def history_native_image_count(history, *, max_messages: int = 24) -> int:
    if not isinstance(history, list) or not history:
        return 0
    n = 0
    for msg in history[-max_messages:]:
        if isinstance(msg, dict):
            n += count_image_blocks_in_content(msg.get("content"))
    return n


def backend_history(llmclient):
    backend = getattr(llmclient, "backend", None)
    backend = getattr(backend, "primary", backend)
    hist = getattr(backend, "history", None) if backend is not None else None
    return hist if isinstance(hist, list) else []


_VISION_FOLLOWUP_RE = re.compile(
    r"(?is)("
    r"重试|再试|再看|重看|再描述|重新描述|"
    r"这图|那图|截图|图片|图里|图中|附图|贴图|识图|看图|"
    r"是什么|啥意思|描述|说说|看看|"
    r"retry|again|describe|what(?:'s| is)? (?:in |on )?(?:the )?(?:image|screenshot|picture)|"
    r"look at (?:the )?(?:image|screenshot|picture)"
    r")"
)


def user_query_looks_like_vision_followup(text: str) -> bool:
    """用户本轮像在追问已贴图片（含「重试」），而非新的纯代码任务。"""
    t = (text or "").strip()
    if not t:
        return False
    if _VISION_FOLLOWUP_RE.search(t):
        return True
    # 极短跟进（如「呢」「呢？」）且不含路径/代码气味
    if len(t) <= 12 and not re.search(r"[\\/]|\.py\b|\.ts\b|def |class |import ", t):
        return True
    return False


def build_vision_direct_answer_sys_prompt(*, image_count: int = 0, via: str = "current") -> str:
    """强制 vision 直答的 system 补丁。

    对齐：
    - Codex view_image：仅当图「尚未」挂在 thread 时才用工具加载；已在上下文则直接看图
      （view_image_spec: "only use if ... image isn't already attached to the thread context"）。
    - Claude Code：粘贴图作为原生 image block 进入 user content，不先走 OCR/搜仓库。

    via:
      - current: 本轮 user content 新挂了 image block
      - history: 本轮无新图，但近期 history 已有原生图（如用户只说「重试」）
    """
    n = max(0, int(image_count or 0))
    n_bit = f"共约 {n} 张原生图片。" if n else "消息/历史中已有原生图片。"
    where = (
        "本条用户消息的 content 里已包含 type=image / image_url 的多模态块（文本里的 [Image #N] 只是 UI 占位符，不是要你去仓库搜索的符号）。"
        if via == "current"
        else "近期对话 history 里已包含原生多模态图片块；用户本轮可能只是「重试/再看/补充一句」，图仍在上下文中。"
    )
    return (
        "\n[Native vision — direct answer]\n"
        f"{where}{n_bit}\n"
        "你必须优先用视觉能力直接阅读这些图片像素并回答用户，遵守：\n"
        "1. 第一轮优先直接作答：描述/对比/回答图中问题；多图时按 [Image #1]、[Image #2]… 或出现顺序分别说明，勿合并成含糊一句。\n"
        "2. 禁止为了「理解 [Image #N] 占位符是什么」而去 code_run / 搜索 ink-ui / 读 imageAttachments.ts；"
        "那是前端附件标签，不等于图片内容。\n"
        "3. 禁止优先 OCR、file_read 本地路径、vision_api、全屏截图；图已在上下文中（同 Codex："
        "已在 thread 中的图不要再 view_image/重复加载）。\n"
        "4. 仅当用户明确要求改代码/查实现，或图片完全无法辨认时，才改用工具；"
        "无法辨认时先说明看不清，再问是否重传或改路径。\n"
        "5. 不要假装看见图却去复述仓库代码；视觉结论必须来自图片本身。\n"
    )


def should_inject_vision_direct_answer(
    *,
    can_image: bool,
    initial_content,
    history,
    user_text: str,
) -> tuple[bool, str, int]:
    """返回 (是否注入, via, image_count)。"""
    if not can_image:
        return False, "", 0
    n_cur = count_image_blocks_in_content(initial_content)
    if n_cur > 0:
        return True, "current", n_cur
    n_hist = history_native_image_count(history)
    if n_hist > 0 and user_query_looks_like_vision_followup(user_text):
        return True, "history", n_hist
    return False, "", 0


def should_flush_display_delta(full_resp, last_pos, chunk):
    return len(full_resp) - last_pos >= STREAM_FLUSH_CHARS or 'LLM Running' in chunk


def normalize_display_assistant_text(full_resp: str) -> str:
    """Idempotent display-only markup rewrites (tests / non-streaming callers).

    Prefer not using this on the live stream path after partial flushes — inserting
    characters before `last_pos` corrupts incremental deltas. The agent run loop
    ships raw stream text and leaves spacing to the frontend formatter.
    """
    if not full_resp:
        return full_resp
    text = full_resp
    if '</summary>' in text:
        text = text.replace('</summary>\n\n', '</summary>')
        text = text.replace('</summary>', '</summary>\n\n')
    if '</file_content>' in text:
        # Wrap bare tags only; leave already-fenced blocks untouched (idempotent).
        parts: list[str] = []
        cursor = 0
        for match in re.finditer(r'<file_content>\s*(.*?)\s*</file_content>', text, flags=re.DOTALL):
            start, end = match.span()
            before = text[max(0, start - 5):start]
            after = text[end:end + 5]
            already_fenced = before.endswith('````\n') or before.endswith('````') and after.startswith('\n````')
            # Also detect fenced form "````\n<file_content>...\n````"
            window_before = text[max(0, start - 6):start]
            window_after = text[end:end + 6]
            already_fenced = window_before.endswith('````\n') and window_after.startswith('\n````')
            parts.append(text[cursor:start])
            if already_fenced:
                parts.append(match.group(0))
            else:
                body = match.group(1)
                parts.append(f'\n````\n<file_content>\n{body}\n</file_content>\n````')
            cursor = end
        parts.append(text[cursor:])
        text = ''.join(parts)
    return text


def load_tool_schema(suffix='', include_mcp_tools=True):
    global TOOLS_SCHEMA
    with open(os.path.join(script_dir, f'assets/tools_schema{suffix}.json'), 'r', encoding='utf-8') as f:
        TS = f.read()
    TOOLS_SCHEMA = json.loads(TS if os.name == 'nt' else TS.replace('powershell', 'bash'))
    if not include_mcp_tools:
        return
    try:
        from mcp_runtime import discover_mcp_tools_cached
        existing = {t.get("function", {}).get("name") for t in TOOLS_SCHEMA}
        for tool in discover_mcp_tools_cached():
            name = tool.get("function", {}).get("name")
            if name and name not in existing:
                TOOLS_SCHEMA.append(tool)
                existing.add(name)
    except Exception as e:
        if os.environ.get("GA_MCP_DEBUG"):
            print(f"[WARN] MCP tool discovery failed: {e}")
load_tool_schema(include_mcp_tools=False)

lang_suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
mem_dir = os.path.join(script_dir, 'memory')
if not os.path.exists(mem_dir): os.makedirs(mem_dir)
mem_txt = os.path.join(mem_dir, 'global_mem.txt')
if not os.path.exists(mem_txt): open(mem_txt, 'w', encoding='utf-8').write('# [Global Memory - L2]\n')
mem_insight = os.path.join(mem_dir, 'global_mem_insight.txt')
if not os.path.exists(mem_insight):
    t = os.path.join(script_dir, f'assets/global_mem_insight_template{lang_suffix}.txt')
    open(mem_insight, 'w', encoding='utf-8').write(open(t, encoding='utf-8').read() if os.path.exists(t) else '')
cdp_cfg = os.path.join(script_dir, 'assets/tmwd_cdp_bridge/config.js')
if not os.path.exists(cdp_cfg):
    try:
        os.makedirs(os.path.dirname(cdp_cfg), exist_ok=True)
        open(cdp_cfg, 'w', encoding='utf-8').write(f"const TID = '__ljq_{hex(random.randint(0, 99999999))[2:8]}';")
    except Exception as e: print(f'[WARN] CDP config init failed: {e} — advanced web features (tmwebdriver) will be unavailable.')

def get_system_prompt(agent=None):
    with open(os.path.join(script_dir, f'assets/sys_prompt{lang_suffix}.txt'), 'r', encoding='utf-8') as f: prompt = f.read()
    prompt += f"\nToday: {time.strftime('%Y-%m-%d %a')}\n"
    prompt += build_ga_project_instructions(script_dir, os.getcwd())
    if not bool(getattr(agent, 'task_dir', None)):
        try:
            from subagent_notifications import build_subagent_notifications_prompt
            prompt += build_subagent_notifications_prompt(script_dir)
        except Exception:
            pass
    prompt += get_global_memory()
    prompt += build_skill_prompt()
    prompt += "\n" + build_agent_role_usage_hint(is_subagent=bool(getattr(agent, 'task_dir', None)), lang_suffix=lang_suffix) + "\n"
    prompt += build_permission_mode_hint(getattr(agent, 'permission_mode', None))
    return prompt


def build_permission_mode_hint(mode):
    '''非 full_access 档时告知模型当前权限边界，避免反复调用会被拒的写/执行工具空转。

    full_access（默认）不注入任何内容，保持历史「工具全开」提示词不变。
    '''
    try:
        from permission_policy import ASK, FULL_ACCESS, READ_ONLY, normalize_permission_mode
    except Exception:
        return ""
    mode = normalize_permission_mode(mode)
    if mode == FULL_ACCESS:
        return ""
    if mode == READ_ONLY:
        return (
            "\n[Permission: Read Only] 当前为只读档。写文件 / 打补丁 / 执行代码 / 执行 JS 等"
            "有副作用的工具会被拒绝（返回 permission deny）。只做读取、检索、分析并直接向用户"
            "汇报；需要改动时说明将要做什么并请用户切换到 Ask 或 Full Access，不要反复重试被拒的工具。\n"
        )
    if mode == ASK:
        return (
            "\n[Permission: Ask for approval] 当前为审批档。读取类工具直接可用；写文件 / 打补丁 /"
            "执行代码 / 执行 JS 等有副作用的工具会阻塞等待用户 accept/deny。"
            "被拒绝时不要盲目重试同一写/执行；调整方案或请用户改用 Full Access。\n"
        )
    return ""

class GenericAgent:
    def __init__(self):
        os.makedirs(os.path.join(script_dir, 'temp'), exist_ok=True)
        # Slice D3：进程内首次构造 agent 时 GC 自产图片缓存（best-effort）
        try:
            from image_gc import maybe_gc_ga_images_on_startup
            maybe_gc_ga_images_on_startup(quiet=True)
        except Exception:
            pass
        self.lock = threading.Lock()
        self.task_dir = None
        self.history = []; self.handler = None;
        self.task_queue = queue.Queue()
        self.is_running = False; self.stop_sig = False
        self.llm_no = 0;  self.inc_out = False; self.verbose = True
        self.peer_hint = True
        # 三档权限：默认 full_access（等于历史「工具全开」行为），可切到 ask / read_only
        from permission_policy import DEFAULT_PERMISSION_MODE
        self.permission_mode = DEFAULT_PERMISSION_MODE
        self.subagent_permission_policy = None
        # ask 档阻塞审批 runtime（可无 emit = headless deny）；bridge 会 set_emit
        try:
            from permission_runtime import PermissionRuntime
            self.permission_runtime = PermissionRuntime()
        except Exception:
            self.permission_runtime = None
        self.log_path = os.path.join(script_dir, f'temp/model_responses/model_responses_{int(time.time()*1e6)%1000000:06d}.txt')
        self.load_llm_sessions()
        self.session_id = None
        self.session_path = None
        self.session_turn_id = 0
        try:
            session_transcript.ensure_agent_session(self)
        except Exception as e:
            print(f"[WARN] Failed to initialize session transcript: {e}")

    def load_llm_sessions(self):
        """阶段 3：只从 llm.yaml 构造会话列表（profiles + mixin），不再读 mykey.py。

        /model 列表项名 = profile 名（backend.name）；默认选中 active_profile。
        yaml 未变且已加载过则跳过重建，保留当前 llm_no 与 history。
        """
        global _llm_yaml_path, _llm_yaml_mtime_ns
        # 热重载：文件未变则复用已有 clients。
        if hasattr(self, "llmclients") and self.llmclients and _llm_yaml_path:
            try:
                if os.path.exists(_llm_yaml_path) and os.stat(_llm_yaml_path).st_mtime_ns == _llm_yaml_mtime_ns:
                    return
            except OSError:
                pass
        try:
            oldhistory = self.llmclient.backend.history
        except Exception:
            oldhistory = None
        try:
            old_name = self.get_llm_name() if hasattr(self, "llmclient") else None
        except Exception:
            old_name = None

        clients, active_index, cfg_path, mtime_ns = load_clients_from_yaml(start_dir=script_dir)
        _llm_yaml_path, _llm_yaml_mtime_ns = cfg_path, mtime_ns
        self.llmclients = clients

        # 尽量按「上次选中的 profile 名」恢复下标；首次加载用 active_profile。
        index = active_index
        if old_name:
            for i, c in enumerate(clients):
                try:
                    if self.get_llm_name(c) == old_name:
                        index = i
                        break
                except Exception:
                    pass
        self.llm_no = index % len(self.llmclients)
        self.llmclient = self.llmclients[self.llm_no]
        if oldhistory:
            try:
                self.llmclient.backend.history = oldhistory
            except Exception:
                pass
        print(
            f"[Info] LLM sessions from {cfg_path}: "
            f"{len(self.llmclients)} profile(s), active=#{self.llm_no} {self.get_llm_name()}"
        )
    
    def next_llm(self, n=-1):
        self.load_llm_sessions()
        index = ((self.llm_no + 1) if n < 0 else int(n)) % len(self.llmclients)
        result = self._switch_llm_index(index)
        if not result.get("ok"):
            raise Exception(result.get("message") or "model switch failed")

    def select_llm(self, selector):
        self.load_llm_sessions()
        selector = str(selector or "").strip()
        if not selector:
            return {"ok": False, "code": "empty", "message": "model selector is empty"}
        if selector.isdigit():
            return self._switch_llm_index(int(selector))
        lowered = selector.lower()
        matches = [
            i for i, client in enumerate(self.llmclients)
            if lowered in self.get_llm_name(client).lower()
            or lowered in getattr(client.backend, "model", "").lower()
            or lowered in getattr(client.backend, "name", "").lower()
        ]
        if len(matches) == 1:
            return self._switch_llm_index(matches[0])
        if not matches:
            return {"ok": False, "code": "not_found", "message": f"model not found: {selector}"}
        return {"ok": False, "code": "ambiguous", "message": f"ambiguous model selector: {selector}"}

    def _switch_llm_index(self, index):
        self.load_llm_sessions()
        if not (0 <= index < len(self.llmclients)):
            return {"ok": False, "code": "out_of_range", "message": f"model index out of range: {index}"}
        lastc = self.llmclient
        self.llm_no = index
        self.llmclient = self.llmclients[self.llm_no]
        try: self.llmclient.backend.history = lastc.backend.history
        except Exception:
            raise Exception('[ERROR] BAD session switch: history 无法迁移到新 backend')
        self.llmclient.last_tools = ''
        name = self.get_llm_name(model=True)
        if 'glm' in name or 'minimax' in name or 'kimi' in name: load_tool_schema('_cn', include_mcp_tools=False)
        else: load_tool_schema(include_mcp_tools=False)
        return {"ok": True, "index": self.llm_no, "name": self.get_llm_name(), "model": self.get_llm_name(model=True)}
    def list_llms(self): 
        self.load_llm_sessions()
        return [(i, self.get_llm_name(b), i == self.llm_no) for i, b in enumerate(self.llmclients)]
    def get_llm_name(self, b=None, model=False):
        b = self.llmclient if b is None else b
        if isinstance(b, dict): return 'BADCONFIG_MIXIN'
        if model: return (getattr(b.backend, 'model', '') or '').lower()
        # 显示成 profile/model（profile 名 = backend.name，来自 llm.yaml）。
        profile = getattr(b.backend, 'name', '') or type(b.backend).__name__
        mdl = getattr(b.backend, 'model', '') or ''
        return f"{profile}/{mdl}" if mdl else profile

    def set_permission_mode(self, mode):
        '''切换主会话权限档：read_only / ask / full_access（默认 full_access）。

        下一轮 put_task 新建 handler 时生效；若当前已有 handler 也同步更新，
        让运行中的会话立刻感知（下一次 dispatch 即按新档评估）。
        '''
        from permission_policy import build_permission_mode_policy, normalize_permission_mode
        mode = normalize_permission_mode(mode)
        self.permission_mode = mode
        if self.handler is not None:
            self.handler.permission_mode_policy = build_permission_mode_policy(mode)
        return mode

    def abort(self):
        if not self.is_running: return
        print('Abort current task...')
        self.stop_sig = True
        # 解开 ask 档挂起的审批（按 deny 收尾），避免 stop 后工具线程永久 wait
        runtime = getattr(self, 'permission_runtime', None)
        if runtime is not None:
            try:
                runtime.cancel_all()
            except Exception as e:
                print(f"[WARN] permission_runtime.cancel_all failed: {e}")
        for target in (getattr(self, 'llmclient', None), getattr(getattr(self, 'llmclient', None), 'backend', None)):
            cancel = getattr(target, 'cancel_current_request', None)
            if cancel:
                try: cancel()
                except Exception as e: print(f"[WARN] cancel_current_request failed: {e}")
        if self.handler is not None: self.handler.cancel()
            
    def put_task(self, query, source="user", images=None):
        display_queue = queue.Queue()
        self.task_queue.put({"query": query, "source": source, "images": images or [], "output": display_queue})
        return display_queue

    # i know it is dangerous, but raw_query is dangerous enough it doesn't enlarge
    def _handle_slash_cmd(self, raw_query, display_queue):
        if not raw_query.startswith('/'): return raw_query
        if _sm := re.match(r'/session\.(\w+)=(.*)', raw_query.strip()):
            k, v = _sm.group(1), _sm.group(2)
            vfile = os.path.join(script_dir, 'temp', v)
            if os.path.isfile(vfile): v = open(vfile, encoding='utf-8').read().strip()
            try: v = json.loads(v)  # cover number parsing
            except (json.JSONDecodeError, ValueError): pass
            setattr(self.llmclient.backend, k, v)
            display_queue.put({'done': smart_format(f"✅ session.{k} = {repr(v)}", max_str_len=500), 'source': 'system'})
            return None
        if _cm := re.match(r'/compact(?:\s+([\s\S]*))?$', raw_query.strip()):
            self._manual_compact(_cm.group(1) or "", display_queue)
            return None
        if raw_query.strip() == '/resume':
            return r'帮我看看最近有哪些会话可以恢复。读model_responses/目录，按修改时间取最近10个文件，从每个文件里找最后一个<history>...</history>块，用一句话总结每个会话在聊什么，列表给我选。注意读文件后要把字面的\n替换成真换行才能正确匹配。'
        return raw_query

    def _manual_compact(self, instructions, display_queue):
        """手动 /compact：用 LLM 摘要替换长历史。CLI/TUI 复用与 ink 相同的核心。"""
        if compact_agent_context is None:
            display_queue.put({'done': "/compact 不可用（compact_context 未加载）", 'source': 'system'})
            return
        result = compact_agent_context(self, instructions=str(instructions or ""))
        if not result.ok:
            display_queue.put({'done': f"压缩失败：{result.message}", 'source': 'system'})
            return
        # 同步刷新日志与会话记录，与 ink_bridge 一致。
        try:
            if replace_log_with_compact_history is not None:
                replace_log_with_compact_history(getattr(self, "log_path", None), copy.deepcopy(self.llmclient.backend.history))
        except Exception: pass
        try:
            if session_transcript is not None and getattr(self, "session_path", None):
                session_transcript.record_compact(
                    self.session_path, session_id=getattr(self, "session_id", ""),
                    message=result.message, backend_history_after=copy.deepcopy(self.llmclient.backend.history))
        except Exception: pass
        display_queue.put({'done': smart_format(result.message, max_str_len=500), 'source': 'system'})

    def run(self):
        while True:
            task = self.task_queue.get()
            raw_query, source, images, display_queue = task["query"], task["source"], task.get("images") or [], task["output"]
            raw_query = self._handle_slash_cmd(raw_query, display_queue)
            if raw_query is None:
                self.task_queue.task_done(); continue
            self.is_running = True
            reset_cancel = getattr(self.llmclient, 'reset_cancel', None)
            if reset_cancel:
                try: reset_cancel()
                except Exception as e: print(f"[WARN] reset_cancel failed: {e}")
            rquery = smart_format(raw_query.replace('\n', ' '), max_str_len=200)
            self.history.append(f"[USER]: {rquery}")
            
            sys_prompt = get_system_prompt(self) + getattr(self.llmclient.backend, 'extra_sys_prompt', '')
            if self.peer_hint: sys_prompt += f"\n[Peer] 用户提及其他会话/后台任务状态时: temp/model_responses/ (只找近期修改的文件尾部)\n"
            handler = GenericAgentHandler(self, self.history, os.path.join(script_dir, 'temp'))
            if self.handler and 'key_info' in self.handler.working: 
                ki = re.sub(r'\n\[SYSTEM\] 此为.*?工作记忆[。\n]*', '', self.handler.working['key_info'])  # 去旧
                handler.working['key_info'] = ki
                handler.working['passed_sessions'] = ps = self.handler.working.get('passed_sessions', 0) + 1
                if ps > 0: handler.working['key_info'] += f'\n[SYSTEM] 此为 {ps} 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n'
            # 三档权限（read_only/ask/full_access，默认 full_access）：给主会话 handler
            # 挂上 mode policy，让 dispatch 在工具执行前按档位放行/拒绝/请求审批。
            # workflow 子 agent 有独立的 workflow_permission_policy，优先级更高，互不影响。
            try:
                from permission_policy import build_permission_mode_policy
                handler.permission_mode_policy = build_permission_mode_policy(
                    getattr(self, 'permission_mode', None)
                )
            except Exception as e:
                print(f"[WARN] Failed to attach permission mode policy: {e}")
            # 进程型 subagent 专用权限隔离；workflow policy 仍由 workflow_child_agent 自己管理。
            handler.subagent_permission_policy = getattr(self, 'subagent_permission_policy', None)
            subagent_tool_before = getattr(self, '_subagent_transcript_tool_before', None)
            if subagent_tool_before is not None:
                handler.tool_before_callback = subagent_tool_before
            subagent_tool_after = getattr(self, '_subagent_transcript_tool_after', None)
            if subagent_tool_after is not None:
                handler.tool_after_callback = subagent_tool_after
            subagent_permission_callback = getattr(self, '_subagent_transcript_permission', None)
            if subagent_permission_callback is not None:
                handler.subagent_permission_event_callback = subagent_permission_callback
            # 共享 agent 级 runtime（含 bridge emit）；每轮 handler 都挂上同一实例
            handler.permission_runtime = getattr(self, 'permission_runtime', None)
            self.handler = handler  # although new handler, the **full** history is in llmclient, so it is full history!
            self.llmclient.log_path = self.log_path
            transcript_history_before = session_transcript.current_backend_history(self)
            can_image = supports_image_input(self.llmclient)
            initial_content = _build_user_content_with_images(raw_query, images) if can_image else None
            if (images or _extract_image_paths(raw_query)) and not can_image:
                # 门控关闭：不把 image block 发给 API（避免 400「不支持 image」）。
                # 附件 path 若不在文本里（常见 [Image #N] 占位），补进 query 供工具/OCR，并给用户友好提示。
                path_hints = []
                for item in image_codec.normalize_image_attachments(images):
                    p = item.get("path") or ""
                    if p and os.path.isfile(p) and p not in path_hints:
                        path_hints.append(p)
                for p in _extract_image_paths(raw_query):
                    if p not in path_hints:
                        path_hints.append(p)
                model_label = ""
                try:
                    model_label = self.get_llm_name(model=True) or ""
                except Exception:
                    model_label = ""
                model_bit = f"「{model_label}」" if model_label else "当前模型"
                try:
                    if path_hints:
                        display_queue.put(
                            f"[image] {model_bit}不支持原生识图，图片未作为多模态内容发送。"
                            f"已把本地路径写入文本，可用 OCR/工具读文件；或 /model 切换到支持 image_input 的模型。\n"
                            f"  paths: {'; '.join(path_hints)}\n"
                        )
                    else:
                        display_queue.put(
                            f"[image] {model_bit}不支持原生识图，图片未发送。"
                            f"请 /model 切换到支持 image_input 的模型，或用文字描述图片内容。\n"
                        )
                except Exception:
                    pass
                if path_hints:
                    # 让模型看到真实路径，避免只剩 [Image #N] 占位却去瞎找文件
                    path_block = "\n".join(f"- {p}" for p in path_hints)
                    raw_query = (
                        f"{raw_query}\n\n"
                        f"[系统] {model_bit}无原生图片输入能力，上述 [Image #N] 仅是占位符，你看不到像素。"
                        f"用户附件本地路径如下（可用 OCR/file_read 等工具，禁止假装已看见图内容）：\n"
                        f"{path_block}\n"
                    )
            # 原生图在「本轮 content」或「近期 history」（如用户只说重试）时，强制 vision 直答。
            # 对齐 Codex：图已在 thread 则不要再工具加载；对齐 CC：粘贴图直接进多模态 user content。
            _inject, _via, _n_img = should_inject_vision_direct_answer(
                can_image=can_image,
                initial_content=initial_content,
                history=backend_history(self.llmclient),
                user_text=raw_query,
            )
            if _inject:
                sys_prompt += build_vision_direct_answer_sys_prompt(image_count=_n_img, via=_via)
            name = self.get_llm_name(model=True)
            from mcp_runtime import mcp_cancellation_scope
            with mcp_cancellation_scope(handler.code_stop_signal):
                load_tool_schema('_cn' if ('glm' in name or 'minimax' in name or 'kimi' in name) else '', include_mcp_tools=True)
            gen = agent_runner_loop(self.llmclient, sys_prompt, raw_query,
                                handler, TOOLS_SCHEMA, max_turns=70, verbose=self.verbose,
                                initial_user_content=initial_content)
            try:
                full_resp = ""; last_pos = 0
                for chunk in gen:
                    if consume_file(self.task_dir, '_stop'): self.abort() 
                    if self.stop_sig: break
                    full_resp += chunk
                    if should_flush_display_delta(full_resp, last_pos, chunk):
                        display_queue.put({'next': full_resp[last_pos:] if self.inc_out else full_resp, 'source': source})
                        last_pos = len(full_resp)
                if self.inc_out and last_pos < len(full_resp): display_queue.put({'next': full_resp[last_pos:], 'source': source})
                # Do NOT rewrite markup only on `done` (e.g. </summary> → </summary>\n\n).
                # Streamed `next` chunks already shipped the raw text; a done-only rewrite
                # makes the full payload diverge from the stream-commit prefix and the
                # Ink UI re-paints Turn 1 + tools a second time. Display spacing belongs
                # in the frontend formatter (formatAssistantText).
                display_queue.put({'done': full_resp, 'source': source})
                subagent_assistant_callback = getattr(self, '_subagent_transcript_assistant', None)
                if subagent_assistant_callback is not None:
                    try: subagent_assistant_callback(full_resp)
                    except Exception: pass
                self.history = handler.history_info
                try:
                    session_transcript.record_agent_turn(
                        self,
                        user_text=raw_query,
                        assistant_text=full_resp,
                        source=source,
                        backend_history_before=transcript_history_before,
                    )
                except Exception as e:
                    print(f"[WARN] Failed to record session transcript: {e}")
            except Exception as e:
                print(f"Backend Error: {format_error(e)}")
                failed_resp = full_resp + f'\n```\n{format_error(e)}\n```'
                display_queue.put({'done': failed_resp, 'source': source})
                subagent_assistant_callback = getattr(self, '_subagent_transcript_assistant', None)
                if subagent_assistant_callback is not None:
                    try: subagent_assistant_callback(failed_resp, error=format_error(e))
                    except Exception: pass
                try:
                    session_transcript.record_agent_turn(
                        self,
                        user_text=raw_query,
                        assistant_text=failed_resp,
                        source=source,
                        backend_history_before=transcript_history_before,
                    )
                except Exception as rec_e:
                    print(f"[WARN] Failed to record session transcript: {rec_e}")
            finally:
                if self.stop_sig: print('User aborted the task.')
                self.is_running = self.stop_sig = False
                self.task_queue.task_done()
                if self.handler is not None: self.handler.cancel()

GeneraticAgent = GenericAgent    

def _subagent_state(task_dir, task_name, nround, turn_status, process_status, output_path=None, final_output_path=None, final_output_sha256=None, last_error=None):
    state_path = os.path.join(task_dir, 'state.json')
    old = {}
    try:
        with open(state_path, encoding='utf-8', errors='replace') as f:
            old = json.load(f)
    except Exception:
        pass
    old.update({
        'schema_version': 1,
        'task_name': task_name,
        'agent_path': old.get('agent_path') or f'/root/{task_name}',
        'pid': os.getpid(),
        'round': int(nround) if isinstance(nround, int) else 0,
        'turn_status': turn_status,
        'process_status': process_status,
        'updated_at': now_iso(),
        'input_path': os.path.join(task_dir, 'input.txt'),
        'output_path': str(output_path) if output_path else old.get('output_path'),
        'final_output_path': str(final_output_path) if final_output_path else old.get('final_output_path'),
        'final_output_sha256': final_output_sha256 if final_output_sha256 else old.get('final_output_sha256'),
        'last_error': last_error,
    })
    if turn_status == 'running':
        old['last_round_started_at'] = old['updated_at']
    if turn_status == 'completed':
        old['last_round_end_at'] = old['updated_at']
    atomic_write_json(state_path, old)
    return old

PARENT_INBOX_EVENT_TYPES = {
    'agent_started',
    'turn_completed',
    'agent_waiting_reply',
    'agent_exited',
    'agent_shutdown',
    'agent_error',
    'agent_closed',
}

def _subagent_transcript_append(task_dir, event_type, payload):
    try:
        state = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
        parent_session_id = state.get('parent_session_id') or payload.get('parent_session_id')
        run_id = state.get('run_id') or payload.get('run_id')
        if parent_session_id and run_id:
            from subagent_transcript import SubagentTranscriptStore
            SubagentTranscriptStore(os.path.join(os.path.dirname(task_dir), 'sessions')).append_event(
                parent_session_id,
                run_id,
                event_type,
                payload,
            )
    except Exception:
        pass


_REALTIME_SUBSCRIBER = None


def _set_realtime_subscriber(subscriber):
    """One process is one agent, so the subscriber is process-wide state.

    _subagent_event is called from eleven places and is the single choke point where this
    process appends something durable — which is exactly when the parent should be woken.
    Threading the subscriber through every call site would add a parameter to all of them to
    say the same thing this module-level handle says once.
    """
    global _REALTIME_SUBSCRIBER
    _REALTIME_SUBSCRIBER = subscriber
    return subscriber


def _signal_parent():
    subscriber = _REALTIME_SUBSCRIBER
    if subscriber is None:
        return False
    try:
        return bool(subscriber.signal())
    except Exception:
        return False


def _subagent_event(task_dir, event):
    append_jsonl_event(os.path.join(task_dir, 'events.jsonl'), event)
    task_name = event.get('task_name') or os.path.basename(os.path.normpath(task_dir))
    state = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
    agent_path = event.get('agent_path') or state.get('agent_path') or f'/root/{task_name}'
    run_id = state.get('run_id') or event.get('run_id')
    payload = {k: v for k, v in event.items() if k not in {'type', 'task_name', 'agent_path', 'run_id'}}
    status = {
        'turn_status': state.get('turn_status'),
        'process_status': state.get('process_status'),
    }
    try:
        from subagent_event_bus import SubagentEventBus
        SubagentEventBus(os.path.join(os.path.dirname(task_dir), 'subagents')).append_event(
            event.get('type') or 'subagent_update',
            agent_path=agent_path,
            run_id=run_id,
            task_name=task_name,
            status=status,
            payload=payload,
            notify=event.get('type') in {'turn_completed', 'agent_exited', 'agent_shutdown', 'agent_error', 'agent_closed'},
        )
    except Exception:
        pass
    try:
        parent_session_id = state.get('parent_session_id') or event.get('parent_session_id')
        if parent_session_id and run_id:
            _subagent_transcript_append(
                task_dir,
                event.get('type') or 'subagent_update',
                {
                    'task_name': task_name,
                    'agent_path': agent_path,
                    'status': status,
                    **payload,
                },
            )
    except Exception:
        pass
    if event.get('type') in PARENT_INBOX_EVENT_TYPES:
        append_parent_inbox_event(task_dir, event)
    # Durable write is done; now poke the parent so its wait doesn't sit out the poll interval.
    _signal_parent()

def _record_child_ipc_status(task_dir, task_name, status, fallback_reason, address=None):
    state_path = os.path.join(task_dir, 'state.json')
    state = read_json_or_none(state_path) or {}
    state['child_ipc_status'] = status
    state['child_ipc_fallback_reason'] = fallback_reason
    state['child_ipc_address'] = address
    atomic_write_json(state_path, state)
    # 'file' is the default transport, so an event there would add a line to every
    # subagent's stream while saying nothing. Only realtime outcomes are worth an event.
    if status != 'file':
        _subagent_event(task_dir, {
            'type': 'child_ipc_status',
            'task_name': task_name,
            'child_ipc_status': status,
            'child_ipc_fallback_reason': fallback_reason,
        })
    return state


def resolve_reply_wait_schedule_from_env(env=None):
    """Read the poll/lifetime knobs from the environment.

    Children are launched by SubagentManager with a fixed command line that carries no
    timing, so the env is the only path that reaches a real spawned subagent. Unset or
    unparseable values fall through to the defaults rather than failing the launch.
    """
    env = os.environ if env is None else env
    overrides = {}
    for key, name in (('GA_SUBAGENT_POLL_INTERVAL_S', 'poll_interval_s'), ('GA_SUBAGENT_IDLE_TIMEOUT_S', 'idle_timeout_s')):
        raw = env.get(key)
        if raw is None or not str(raw).strip():
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        overrides[name] = value
    return overrides


def _reply_wait_slices(interval, timeout, max_iterations, monotonic_fn):
    """Yield each wait duration for one reply-wait window.

    Deadline-driven so the poll interval can change without changing the lifetime; the last
    slice is clamped to the deadline so a coarse interval cannot overshoot it.
    """
    if max_iterations is not None:
        for _ in range(max_iterations):
            yield interval
        return
    deadline = monotonic_fn() + timeout
    while True:
        remaining = deadline - monotonic_fn()
        # Epsilon guard: float accumulation of many small slices can land a hair short of
        # the deadline and buy a spurious extra round.
        if remaining <= 1e-9:
            return
        yield min(interval, remaining)


def resolve_reply_wait_schedule(reply_wait_iterations, reply_sleep_s, poll_interval_s, idle_timeout_s):
    """Separate wake-up granularity from total idle lifetime.

    These used to be one knob: reply_wait_iterations x reply_sleep_s set both, so halving
    the latency also halved how long an idle subagent stayed alive. Now poll_interval_s
    controls granularity and idle_timeout_s controls lifetime. The legacy iteration params
    are still honoured verbatim so existing callers and tests keep their exact timing.
    """
    interval = float(poll_interval_s) if poll_interval_s is not None else float(reply_sleep_s)
    if poll_interval_s is None and idle_timeout_s is None:
        timeout = float(reply_sleep_s) * int(reply_wait_iterations)
    elif idle_timeout_s is not None:
        timeout = float(idle_timeout_s)
    else:
        timeout = float(reply_sleep_s) * int(reply_wait_iterations)
    if interval <= 0:
        # A zero interval means "wake immediately"; lifetime then has to stay bounded by the
        # iteration count, otherwise a deadline-driven loop would spin forever.
        return 0.0, timeout, int(reply_wait_iterations)
    return interval, timeout, None


def run_task_worker_loop(agent, task_dir, input_text=None, reply_wait_iterations=300, reply_sleep_s=2, sleep_fn=time.sleep, permission_profile=None, permission_options=None, parent_permission_mode=None, poll_interval_s=None, idle_timeout_s=None, monotonic_fn=time.monotonic):
    task_dir = str(task_dir)
    task_name = os.path.basename(os.path.normpath(task_dir))
    poll_interval, idle_timeout, max_wait_iterations = resolve_reply_wait_schedule(
        reply_wait_iterations, reply_sleep_s, poll_interval_s, idle_timeout_s
    )
    agent.peer_hint = False
    agent.task_dir = task_dir
    if permission_profile is not None or permission_options is not None or parent_permission_mode is not None:
        state_path = os.path.join(task_dir, 'state.json')
        state = read_json_or_none(state_path) or {}
        if permission_profile is not None:
            state['permission_profile'] = permission_profile
        if permission_options is not None:
            state['permission_options'] = permission_options
        if parent_permission_mode is not None:
            state['parent_permission_mode'] = parent_permission_mode
        atomic_write_json(state_path, state)
    try:
        from subagent_permissions import load_subagent_permission_policy
        agent.subagent_permission_policy = load_subagent_permission_policy(task_dir)
    except Exception as e:
        print(f"[WARN] Failed to attach subagent permission policy: {e}")
    state_path = os.path.join(task_dir, 'state.json')
    existing_state = read_json_or_none(state_path) or {}
    # Realtime subscription is an accelerator only: it carries change signals, while
    # mailbox.jsonl stays the authoritative source. A child that cannot subscribe keeps
    # polling, but records why — a silent child-side failure is how the never-connected
    # channel stayed hidden.
    realtime_subscriber = None
    child_ipc_status, child_ipc_fallback_reason = 'file', None
    try:
        from subagent_realtime_ipc import resolve_child_subscription
        realtime_subscriber, child_ipc_status, child_ipc_fallback_reason = resolve_child_subscription(task_dir, existing_state)
    except Exception as e:
        realtime_subscriber = None
        child_ipc_status, child_ipc_fallback_reason = 'fallback', format_error(e)
    _set_realtime_subscriber(realtime_subscriber)
    existing_state = _record_child_ipc_status(
        task_dir, task_name, child_ipc_status, child_ipc_fallback_reason, getattr(realtime_subscriber, 'address', None)
    )
    nround = int(existing_state.get('round') or 0) if existing_state.get('round') else ''
    infile = os.path.join(task_dir, 'input.txt')
    if input_text:
        os.makedirs(task_dir, exist_ok=True)
        import glob
        [os.remove(f) for f in glob.glob(os.path.join(task_dir, 'output*.txt'))]
        with open(infile, 'w', encoding='utf-8') as f: f.write(input_text)
        nround = ''
    if (fh := consume_file(task_dir, '_history.json')):
        agent.llmclient.backend.history = json.loads(fh)
    with open(infile, encoding='utf-8') as f: raw = f.read()
    _subagent_transcript_append(task_dir, 'request', {'task_name': task_name, 'prompt': raw})
    while True:
        output_path = os.path.join(task_dir, f'output{nround}.txt')
        try:
            _subagent_state(task_dir, task_name, nround, 'running', 'alive', output_path=output_path)
            _subagent_event(task_dir, {'type': 'turn_started', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})

            def _transcript_tool_before(tool_name, args, response):
                _subagent_transcript_append(
                    task_dir,
                    'tool_call',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'tool_name': tool_name,
                        'args': {k: v for k, v in (args or {}).items() if not str(k).startswith('_')},
                    },
                )

            def _transcript_tool_after(tool_name, args, response, ret):
                data = getattr(ret, 'data', ret)
                if not isinstance(data, (dict, list, str, int, float, bool, type(None))):
                    data = {'content': getattr(data, 'content', str(data))}
                _subagent_transcript_append(
                    task_dir,
                    'tool_result',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'tool_name': tool_name,
                        'data': data,
                    },
                )

            def _transcript_permission(tool_name, args, decision):
                _subagent_transcript_append(
                    task_dir,
                    'permission_decision',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'tool_name': tool_name,
                        'decision': decision.to_dict(),
                    },
                )

            def _transcript_assistant(content, error=None):
                payload = {
                    'task_name': task_name,
                    'round': int(nround) if isinstance(nround, int) else 0,
                    'content': content,
                }
                if error:
                    payload['error'] = error
                _subagent_transcript_append(task_dir, 'assistant', payload)

            agent._subagent_transcript_tool_before = _transcript_tool_before
            agent._subagent_transcript_tool_after = _transcript_tool_after
            agent._subagent_transcript_permission = _transcript_permission
            agent._subagent_transcript_assistant = _transcript_assistant
            dq = agent.put_task(raw, source='task')
            while 'done' not in (item := dq.get(timeout=300)):
                if 'next' in item and random.random() < 0.95:
                    with open(output_path, 'w', encoding='utf-8') as f: f.write(item.get('next', ''))
                    _subagent_event(task_dir, {'type': 'output_snapshot', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0, 'output_path': output_path})
            with open(output_path, 'w', encoding='utf-8') as f: f.write(item['done'] + '\n\n[ROUND END]\n')
            digest = sha256_file(output_path)
            state_before_final = read_json_or_none(os.path.join(task_dir, 'state.json')) or {}
            final_output_ref = None
            try:
                from subagent_artifacts import SubagentArtifactStore
                run_id = state_before_final.get('run_id') or f"legacy_{task_name}"
                artifact_dir = state_before_final.get('artifact_dir') or os.path.join(os.path.dirname(task_dir), 'subagents', 'runs', run_id)
                artifact = SubagentArtifactStore(artifact_dir).record_final_output(output_path, round_no=int(nround) if isinstance(nround, int) else 0)
                final_output_ref = artifact.get('artifact_id')
                state_before_final['artifact_dir'] = artifact_dir
                state_before_final['final_output_ref'] = final_output_ref
                atomic_write_json(os.path.join(task_dir, 'state.json'), state_before_final)
            except Exception:
                pass
            try:
                _subagent_transcript_append(
                    task_dir,
                    'final_output',
                    {
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'output_path': output_path,
                        'sha256': digest,
                        'artifact_id': final_output_ref,
                        'final_output': item['done'],
                    },
                )
            except Exception:
                pass
            _subagent_state(task_dir, task_name, nround, 'completed', 'waiting_reply', output_path=output_path, final_output_path=output_path, final_output_sha256=digest)
            _subagent_event(task_dir, {'type': 'turn_completed', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0, 'output_path': output_path, 'sha256': digest, 'final_output_ref': final_output_ref})
            _subagent_event(task_dir, {'type': 'agent_waiting_reply', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
            consume_file(task_dir, '_stop')
            stop_requested = False
            for wait_s in _reply_wait_slices(poll_interval, idle_timeout, max_wait_iterations, monotonic_fn):
                if realtime_subscriber is not None and not realtime_subscriber.closed:
                    # A signal carries no payload; it only means "go re-read the durable
                    # mailbox now" instead of waiting out the poll interval.
                    realtime_subscriber.wait(wait_s)
                else:
                    sleep_fn(wait_s)
                if realtime_subscriber is not None and realtime_subscriber.closed and child_ipc_status != 'fallback':
                    # The parent tore the channel down mid-run. Checked here rather than inside
                    # the wait branch because the disconnect is now just as likely to surface on
                    # an outbound signal from _subagent_event. Polling still delivers, but the
                    # degradation has to be visible or it looks healthy.
                    child_ipc_status, child_ipc_fallback_reason = 'fallback', 'realtime channel disconnected mid-run; durable file event bus stays the transport'
                    _record_child_ipc_status(task_dir, task_name, child_ipc_status, child_ipc_fallback_reason)
                if consume_file(task_dir, '_stop'):
                    _subagent_state(task_dir, task_name, nround, 'completed', 'shutdown', output_path=output_path, final_output_path=output_path, final_output_sha256=digest)
                    _subagent_event(task_dir, {'type': 'agent_shutdown', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
                    stop_requested = True
                    break
                try:
                    from subagent_mailbox import SubagentMailbox
                    mailbox_result = SubagentMailbox(os.path.join(task_dir, 'mailbox.jsonl')).consume_trigger_turn()
                except Exception:
                    mailbox_result = None
                if mailbox_result is not None:
                    raw = mailbox_result.get('content') or ''
                    consume_file(task_dir, 'reply.txt')
                    _subagent_event(task_dir, {
                        'type': 'message_consumed',
                        'task_name': task_name,
                        'round': int(nround) if isinstance(nround, int) else 0,
                        'source': 'mailbox',
                        'message_ids': [row.get('message_id') for row in mailbox_result.get('messages') or []],
                    })
                    break
                if (raw := consume_file(task_dir, 'reply.txt')):
                    _subagent_event(task_dir, {'type': 'message_consumed', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
                    break
            else:
                _subagent_state(task_dir, task_name, nround, 'completed', 'exited', output_path=output_path, final_output_path=output_path, final_output_sha256=digest)
                _subagent_event(task_dir, {'type': 'agent_exited', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0})
                break
            if stop_requested:
                break
            nround = nround + 1 if isinstance(nround, int) else 1
        except Exception as e:
            _subagent_state(task_dir, task_name, nround, 'errored', 'exited', output_path=output_path, last_error=format_error(e))
            _subagent_event(task_dir, {'type': 'agent_error', 'task_name': task_name, 'round': int(nround) if isinstance(nround, int) else 0, 'error': format_error(e)})
            if realtime_subscriber is not None:
                realtime_subscriber.close()
            _set_realtime_subscriber(None)
            raise
    if realtime_subscriber is not None:
        realtime_subscriber.close()
    _set_realtime_subscriber(None)

def start_task_background(
    task_name,
    input_text=None,
    *,
    llm_no=0,
    verbose=False,
    root_dir=None,
    popen=None,
    python_executable=None,
    fork_turns=None,
    fork_history=None,
    permission_profile=None,
    parent_permission_mode=None,
    permission_options=None,
):
    from subagent_manager import SubagentManager

    root_dir = os.path.abspath(root_dir or script_dir)
    manager = SubagentManager(root_dir=root_dir, popen=popen, python_executable=python_executable or sys.executable)
    task_name = manager._task_name_from_target(task_name)
    if fork_turns is None:
        fork_turns = 'all' if fork_history is not None else 'none'
    fork_mode, history_to_write = manager._select_fork_history(fork_turns, fork_history)
    from subagent_permissions import normalize_permission_metadata
    permission_metadata = normalize_permission_metadata(
        {'permission_profile': permission_profile, 'permission_options': permission_options or {}, 'parent_permission_mode': parent_permission_mode}
    )
    task_dir = os.path.join(root_dir, 'temp', task_name)
    os.makedirs(task_dir, exist_ok=True)
    input_path = os.path.join(task_dir, 'input.txt')
    output_path = os.path.join(task_dir, 'output.txt')
    if input_text is not None:
        import glob

        for old_output in glob.glob(os.path.join(task_dir, 'output*.txt')):
            try:
                os.remove(old_output)
            except OSError:
                pass
        with open(input_path, 'w', encoding='utf-8') as f:
            f.write(input_text)
    history_path = os.path.join(task_dir, '_history.json')
    if os.path.exists(history_path):
        os.remove(history_path)
    if history_to_write is not None:
        atomic_write_json(history_path, history_to_write)
    state = {
        'schema_version': 1,
        'task_name': task_name,
        'agent_path': f'/root/{task_name}',
        'parent_session_id': None,
        'pid': None,
        'round': 0,
        'turn_status': 'pending',
        'process_status': 'starting',
        'started_at': now_iso(),
        'updated_at': now_iso(),
        'input_path': input_path,
        'output_path': output_path,
        'final_output_path': None,
        'final_output_sha256': None,
        'last_message': input_text,
        'last_error': None,
        'close_reason': None,
        'fork_turns': fork_mode,
        'permission_profile': permission_metadata['permission_profile'],
        'parent_permission_mode': permission_metadata.get('parent_permission_mode'),
        'permission_options': permission_metadata['options'],
    }
    state_path = os.path.join(task_dir, 'state.json')
    atomic_write_json(state_path, state)

    cmd = [
        manager.python_executable,
        os.path.abspath(__file__),
        '--task',
        task_name,
        '--nobg',
        '--task_root',
        root_dir,
        '--llm_no',
        str(llm_no),
        '--permission_profile',
        permission_metadata['permission_profile'],
        '--parent_permission_mode',
        permission_metadata.get('parent_permission_mode') or '',
        '--permission_options',
        json.dumps(permission_metadata['options'], ensure_ascii=False, sort_keys=True),
    ]
    if verbose:
        cmd.append('--verbose')

    stdout = open(os.path.join(task_dir, 'stdout.log'), 'w', encoding='utf-8')
    stderr = open(os.path.join(task_dir, 'stderr.log'), 'w', encoding='utf-8')
    try:
        kwargs = {'cwd': root_dir, 'stdout': stdout, 'stderr': stderr}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x08000000
        proc = (popen or __import__('subprocess').Popen)(cmd, **kwargs)
    except Exception as e:
        state.update({'turn_status': 'errored', 'process_status': 'exited', 'last_error': format_error(e), 'updated_at': now_iso()})
        atomic_write_json(state_path, state)
        _subagent_event(task_dir, {'type': 'agent_error', 'task_name': task_name, 'round': 0, 'error': format_error(e)})
        manager.register_agent(task_name, state, task_dir)
        raise
    finally:
        stdout.close()
        stderr.close()

    pid = getattr(proc, 'pid', None)
    state.update({'pid': pid, 'process_status': 'alive', 'updated_at': now_iso()})
    atomic_write_json(state_path, state)
    _subagent_event(task_dir, {'type': 'agent_started', 'task_name': task_name, 'pid': pid})
    manager.register_agent(task_name, state, task_dir)
    return pid

def _load_permission_options_arg(value):
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_fork_history_arg(value):
    if not value:
        return None
    source = value[1:] if value.startswith('@') else value
    if os.path.exists(source):
        with open(source, 'r', encoding='utf-8') as f:
            payload = f.read()
    else:
        payload = value
    return json.loads(payload)

if __name__ == '__main__':
    import argparse
    from datetime import datetime
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', metavar='IODIR', help='一次性任务模式(文件IO)')
    parser.add_argument('--reflect', metavar='SCRIPT', help='反射模式：加载监控脚本，check()触发时发任务')
    parser.add_argument('--input', help='prompt')
    parser.add_argument('--task_root', default=script_dir, help='子智能体任务根目录')
    parser.add_argument('--llm_no', type=int, default=0)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--nobg', action='store_true')
    parser.add_argument('--fork_turns', default=None, help='context fork mode: none, all, or a positive integer')
    parser.add_argument('--fork_history', default=None, help='context fork history as JSON, a JSON file path, or @file')
    parser.add_argument('--permission_profile', default=None, help='subagent permission profile')
    parser.add_argument('--parent_permission_mode', default=None, help='parent permission mode for inherit-current-permissions')
    parser.add_argument('--permission_options', default=None, help='subagent permission options JSON')
    args, _unknown = parser.parse_known_args()
    _reflect_args = dict(zip([k.lstrip('-') for k in _unknown[::2]], _unknown[1::2])) if _unknown else {}

    if args.task and not args.nobg:
        fork_history = _load_fork_history_arg(args.fork_history)
        print(start_task_background(
            args.task,
            input_text=args.input,
            llm_no=args.llm_no,
            verbose=args.verbose,
            root_dir=args.task_root,
            fork_turns=args.fork_turns,
            fork_history=fork_history,
            permission_profile=args.permission_profile,
            parent_permission_mode=args.parent_permission_mode,
            permission_options=_load_permission_options_arg(args.permission_options),
        )); sys.exit(0)

    agent = GeneraticAgent()
    agent.next_llm(args.llm_no)
    agent.verbose = args.verbose
    threading.Thread(target=agent.run, daemon=True).start()

    if args.task:
        task_root = os.path.abspath(args.task_root or script_dir)
        run_task_worker_loop(
            agent,
            os.path.join(task_root, f'temp/{args.task}'),
            input_text=args.input,
            permission_profile=args.permission_profile,
            parent_permission_mode=args.parent_permission_mode,
            permission_options=_load_permission_options_arg(args.permission_options),
            **resolve_reply_wait_schedule_from_env(),
        )
    elif args.reflect:
        agent.peer_hint = False
        import importlib.util
        spec = importlib.util.spec_from_file_location('reflect_script', args.reflect)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        if hasattr(mod, 'init'): mod.init(_reflect_args)
        _mt = os.path.getmtime(args.reflect)
        print(f'[Reflect] loaded {args.reflect}' + (f' args={_reflect_args}' if _reflect_args else ''))
        while True:
            if os.path.getmtime(args.reflect) != _mt:
                try:
                    spec.loader.exec_module(mod); _mt = os.path.getmtime(args.reflect)
                    if hasattr(mod, 'init'): mod.init(_reflect_args)
                    print('[Reflect] reloaded')
                except Exception as e: print(f'[Reflect] reload error: {e}')
            time.sleep(getattr(mod, 'INTERVAL', 5))
            try: task = mod.check()
            except Exception as e: 
                print(f'[Reflect] check() error: {e}'); continue
            if task and task == '/exit': break
            if task is None: continue
            print(f'[Reflect] triggered: {task[:80]}')
            dq = agent.put_task(task, source='reflect')
            try:
                while 'done' not in (item := dq.get(timeout=180)): pass
                result = item['done']
                print(result)
            except Exception as e:
                if getattr(mod, 'ONCE', False): raise
                print(f'[Reflect] drain error: {e}'); result = f'[ERROR] {e}'
            log_dir = os.path.join(script_dir, 'temp/reflect_logs'); os.makedirs(log_dir, exist_ok=True)
            script_name = os.path.splitext(os.path.basename(args.reflect))[0]
            open(os.path.join(log_dir, f'{script_name}_{datetime.now():%Y-%m-%d}.log'), 'a', encoding='utf-8').write(f'[{datetime.now():%m-%d %H:%M}]\n{result}\n\n')
            if (on_done := getattr(mod, 'on_done', None)):
                try: on_done(result)
                except Exception as e: print(f'[Reflect] on_done error: {e}')
            if getattr(mod, 'ONCE', False): print('[Reflect] ONCE=True, exiting.'); break
    else:
        try: import readline
        except Exception: pass
        agent.inc_out = True
        while True:
            q = input('> ').strip()
            if not q: continue
            try:
                dq = agent.put_task(q, source='user')
                while True:
                    item = dq.get()
                    if 'next' in item: print(item['next'], end='', flush=True)
                    if 'done' in item: print(); break
            except KeyboardInterrupt:
                agent.abort()
                print('\n[Interrupted]')
